#! /usr/bin/env python
#
#	synctool	WJ103
#
#   synctool Copyright 2013 Walter de Jong <walter@heiho.net>
#
#   synctool COMES WITH NO WARRANTY. synctool IS FREE SOFTWARE.
#   synctool is distributed under terms described in the GNU General Public
#   License.
#

import os
import sys
import time
import shlex
import getopt
import errno
import subprocess

import synctool.config
import synctool.lib
from synctool.lib import verbose, stdout, stderr, terse, unix_out, dryrun_msg
import synctool.overlay
import synctool.param
import synctool.syncstat

# get_options() returns these action codes
ACTION_DEFAULT = 0
ACTION_DIFF = 1
ACTION_ERASE_SAVED = 2
ACTION_REFERENCE = 3

SINGLE_FILES = []
DIR_CHANGED = None


def run_command(cmd):
	'''run a shell command'''

	# a command can have arguments
	arr = shlex.split(cmd)
	cmdfile = arr[0]

	statbuf = synctool.syncstat.SyncStat(cmdfile)
	if not statbuf.exists():
		stderr('error: command %s not found' %
				synctool.lib.prettypath(cmdfile))
		return

	if not statbuf.is_exec():
		stderr("warning: file '%s' is not executable" %
				synctool.lib.prettypath(cmdfile))
		return

	# run the shell command
	synctool.lib.shell_command(cmd)


def run_command_in_dir(dest_dir, cmd):
	'''change directory to dest_dir, and run the shell command'''

	verbose('  os.chdir(%s)' % dest_dir)
	unix_out('cd %s' % dest_dir)

	cwd = os.getcwd()

	# if dry run, the target directory may not exist yet
	# (mkdir has not been called for real, for a dry run)
	if synctool.lib.DRY_RUN:
		run_command(cmd)

		verbose('  os.chdir(%s)' % cwd)
		unix_out('cd %s' % cwd)
		unix_out('')
		return

	try:
		os.chdir(dest_dir)
	except OSError, reason:
		stderr('error changing directory to %s: %s' % (dest_dir, reason))
	else:
		run_command(cmd)

		verbose('  os.chdir(%s)' % cwd)
		unix_out('cd %s' % cwd)
		unix_out('')

		try:
			os.chdir(cwd)
		except OSError, reason:
			stderr('error changing directory to %s: %s' % (cwd, reason))


def _run_post(obj, post_script):
	'''run the .post script that goes with the object'''

	if synctool.lib.NO_POST:
		return

	if not post_script:
		return

	# temporarily restore original umask
	# so the script runs with the umask set by the sysadmin
	os.umask(synctool.param.ORIG_UMASK)

	if obj.dest_stat.is_dir():
		# run in the directory itself
		run_command_in_dir(obj.dest_path, post_script)
	else:
		# run in the directory where the file is
		run_command_in_dir(os.path.dirname(obj.dest_path), post_script)

	os.umask(077)


def _overlay_callback(obj, post_dict, dir_changed=False):
	'''compare files and run post-script if needed'''

	verbose('checking %s' % obj.print_src())

	if obj.src_stat.is_dir():
		if not obj.check():
			dir_changed = True

		if dir_changed and post_dict.has_key(obj.dest_path):
			_run_post(obj, post_dict[obj.dest_path])

		return True, dir_changed

	if not obj.check() and post_dict.has_key(obj.dest_path):
		_run_post(obj, post_dict[obj.dest_path])
		return True, True

	return True, False


def overlay_files():
	'''run the overlay function'''

	synctool.overlay.visit(synctool.param.OVERLAY_DIR, _overlay_callback)


def _delete_callback(obj, post_dict, dir_changed=False):
	'''delete files'''

	# don't delete directories
	if obj.src_stat.is_dir():
#		verbose('refusing to delete directory %s' % (obj.dest_path + os.sep))
		if dir_changed and post_dict.has_key(obj.dest_path):
			_run_post(obj, post_dict[obj.dest_path])

		return True, dir_changed

	if obj.dest_stat.is_dir():
		stderr('destination is a directory: %s, skipped' % obj.print_src())
		return True, False

	verbose('checking %s' % obj.print_src())

	if obj.dest_stat.exists():
		vnode = obj.vnode_dest_obj()
		vnode.harddelete()

		if post_dict.has_key(obj.dest_path):
			_run_post(obj, post_dict[obj.dest_path])
		return True, True

	return True, False


def delete_files():
	'''run the delete/ dir'''

	synctool.overlay.visit(synctool.param.DELETE_DIR, _delete_callback)


def _erase_saved_callback(obj, post_dict, dir_changed=False):
	'''erase *.saved backup files'''

	if obj.src_stat.is_dir():
		# run .post script on changed directory
		if dir_changed and post_dict.has_key(obj.dest_path):
			_run_post(obj, post_dict[obj.dest_path])

		return True, dir_changed

	obj.dest_path += '.saved'
	obj.dest_stat = synctool.syncstat.SyncStat(obj.dest_path)

	# .saved directories will be removed, but only when they are empty

	if obj.dest_stat.exists():
		vnode = obj.vnode_dest_obj()
		vnode.harddelete()
		return True, True

	return True, False


def erase_saved():
	'''List and delete *.saved backup files'''

	synctool.overlay.visit(synctool.param.OVERLAY_DIR, _erase_saved_callback)
	synctool.overlay.visit(synctool.param.DELETE_DIR, _erase_saved_callback)


def _single_overlay_callback(obj, post_dict, updated=False):
	'''do overlay function for single files'''

	go_on = True

	# TODO match terse path
	if obj.dest_path in SINGLE_FILES:
		SINGLE_FILES.remove(obj.dest_path)
		ok, updated = _overlay_callback(obj, post_dict)
		if not updated:
			stdout('%s is up to date' % obj.dest_path)
			terse(synctool.lib.TERSE_OK, obj.dest_path)
			unix_out('# %s is up to date\n' % obj.dest_path)
		else:
			# register .post on the parent dir, if it has a .post script
			obj.dest_path = os.path.dirname(obj.dest_path)
			obj.dest_stat = synctool.syncstat.SyncStat(obj.dest_path)

			if post_dict.has_key(obj.dest_path):
				DIR_CHANGED[obj.dest_path] = (obj, post_dict[obj.dest_path])

		if not SINGLE_FILES:
			return False, updated

	return go_on, updated


def _single_delete_callback(obj, post_dict, updated=False):
	'''do delete function for single files'''

	go_on = True

	# TODO match terse path
	if obj.dest_path in SINGLE_FILES:
		SINGLE_FILES.remove(obj.dest_path)
		ok, updated = _delete_callback(obj, post_dict)
		if updated:
			# register .post on the parent dir, if it has a .post script
			obj.dest_path = os.path.dirname(obj.dest_path)
			obj.dest_stat = synctool.syncstat.SyncStat(obj.dest_path)

			if post_dict.has_key(obj.dest_path):
				DIR_CHANGED[obj.dest_path] = (obj, post_dict[obj.dest_path])

		if not SINGLE_FILES:
			return False, updated

	return go_on, updated


def single_files():
	'''check/update a list of single files'''

	global DIR_CHANGED

	DIR_CHANGED = {}
	synctool.overlay.visit(synctool.param.OVERLAY_DIR,
							_single_overlay_callback)

	# run any .post scripts on updated directories
	for path in DIR_CHANGED:
		obj, post_script = DIR_CHANGED[path]
		_run_post(obj, post_script)

	if len(SINGLE_FILES) > 0:
		DIR_CHANGED = {}

		# there are still single files left
		# maybe they are in the delete tree?
		synctool.overlay.visit(synctool.param.DELETE_DIR,
								_single_delete_callback)

		# run any .post scripts on updated directories
		# (it's really correct to do this twice; once overlay/, once delete/)
		for path in DIR_CHANGED:
			obj, post_script = DIR_CHANGED[path]
			_run_post(obj, post_script)

	for filename in SINGLE_FILES:
		stderr('%s is not in the overlay tree' % filename)


def _single_erase_saved_callback(obj, post_dict, updated=False):
	'''do 'erase saved' function for single files'''

	go_on = True

	# TODO match terse path
	if obj.dest_path in SINGLE_FILES:
		SINGLE_FILES.remove(obj.dest_path)
		is_dest = True
	else:
		is_dest = False

	# maybe the user supplied a '.saved' filename
	filename = obj.dest_path + '.saved'
	if filename in SINGLE_FILES:
		SINGLE_FILES.remove(filename)
		is_saved = True
	else:
		is_saved = False

	if is_dest or is_saved:
		ok, updated = _erase_saved_callback(obj, post_dict)
		if updated:
			# register .post on the parent dir, if it has a .post script
			obj.dest_path = os.path.dirname(obj.dest_path)
			obj.dest_stat = synctool.syncstat.SyncStat(obj.dest_path)

			if post_dict.has_key(obj.dest_path):
				DIR_CHANGED[obj.dest_path] = (obj, post_dict[obj.dest_path])

		if not SINGLE_FILES:
			return False, updated

	return go_on, updated


def single_erase_saved():
	'''erase single backup files'''

	# maybe the user supplied a '.saved' filename
	(name, ext) = os.path.splitext(filename)
	if ext == '.saved':
		filename = name

	global DIR_CHANGED

	DIR_CHANGED = {}
	synctool.overlay.visit(synctool.param.OVERLAY_DIR,
							_single_erase_saved_callback)

	# run any .post scripts on updated directories
	for path in DIR_CHANGED:
		obj, post_script = DIR_CHANGED[path]
		_run_post(obj, post_script)

	if len(SINGLE_FILES) > 0:
		DIR_CHANGED = {}

		# there are still single files left
		# maybe they are in the delete tree?
		synctool.overlay.visit(synctool.param.DELETE_DIR,
								_single_erase_saved_callback)

		# run any .post scripts on updated directories
		# (it's really correct to do this twice; once overlay/, once delete/)
		for path in DIR_CHANGED:
			obj, post_script = DIR_CHANGED[path]
			_run_post(obj, post_script)

	for filename in SINGLE_FILES:
		stderr('%s is not in the overlay tree' % filename)


def _reference_callback(obj, post_dict, dir_changed=False):
	# TODO match terse path
	if obj.dest_path in SINGLE_FILES:
		SINGLE_FILES.remove(obj.dest_path)

		print obj.print_src()

		if not SINGLE_FILES:
			return False, False

	return True, False


def reference_files():
	'''show which source file in the repository synctool uses'''

	# reference() can't find ._template.post scripts
	# Is this a bad thing?

	synctool.overlay.visit(synctool.param.OVERLAY_DIR, _reference_callback)

	for filename in SINGLE_FILES:
		stderr('%s is not in the overlay tree' % filename)


def _diff_callback(obj, post_dict, dir_changed=False):
	# TODO match terse path
	if obj.dest_path in SINGLE_FILES:
		SINGLE_FILES.remove(obj.dest_path)

		verbose('%s %s %s' % (synctool.param.DIFF_CMD,
								obj.dest_path, obj.print_src()))
		unix_out('%s %s %s' % (synctool.param.DIFF_CMD,
								obj.dest_path, obj.src_path))
		# execute diff
		sys.stdout.flush()
		sys.stderr.flush()

		cmd_arr = shlex.split(synctool.param.DIFF_CMD)
		cmd_arr.append(obj.dest_path)
		cmd_arr.append(obj.src_path)
		try:
			subprocess.call(cmd_arr, shell=False)
		except OSError, reason:
			stderr('failed to run diff_cmd: %s' % reason)

		sys.stdout.flush()
		sys.stderr.flush()

		if not SINGLE_FILES:
			return False, False

	return True, False


def diff_files():
	'''display a diff of the single files'''

	synctool.overlay.visit(synctool.param.OVERLAY_DIR, _diff_callback)

	for filename in SINGLE_FILES:
		stderr('%s is not in the overlay tree' % filename)


def be_careful_with_getopt():
	'''check sys.argv for dangerous common typo's on the command-line'''

	# be extra careful with possible typo's on the command-line
	# because '-f' might run --fix because of the way that getopt() works

	for arg in sys.argv:
		# This is probably going to give stupid-looking output
		# in some cases, but it's better to be safe than sorry

		if arg[:2] == '-d' and arg.find('f') > -1:
			print "Did you mean '--diff'?"
			sys.exit(1)

		if arg[:2] == '-r' and arg.find('f') > -1:
			if arg.count('e') >= 2:
				print "Did you mean '--reference'?"
			else:
				print "Did you mean '--ref'?"
			sys.exit(1)


def	option_combinations(opt_diff, opt_single, opt_reference, opt_erase_saved,
	opt_upload, opt_suffix, opt_fix):

	'''some combinations of command-line options don't make sense;
	alert the user and abort'''

	if opt_erase_saved and (opt_diff or opt_reference or opt_upload):
		stderr("option --erase-saved can not be combined with other actions")
		sys.exit(1)

	if opt_upload and (opt_diff or opt_single or opt_reference):
		stderr("option --upload can not be combined with other actions")
		sys.exit(1)

	if opt_suffix and not opt_upload:
		stderr("option --suffix can only be used together with --upload")
		sys.exit(1)

	if opt_diff and (opt_single or opt_reference or opt_fix):
		stderr("option --diff can not be combined with other actions")
		sys.exit(1)

	if opt_reference and (opt_single or opt_fix):
		stderr("option --reference can not be combined with other actions")
		sys.exit(1)


def check_cmd_config():
	'''check whether the commands as given in synctool.conf actually exist'''

	(ok, synctool.param.DIFF_CMD) = synctool.config.check_cmd_config(
										'diff_cmd', synctool.param.DIFF_CMD)
	if not ok:
		sys.exit(-1)


def usage():
	print 'usage: %s [options] [<arguments>]' % os.path.basename(sys.argv[0])
	print 'options:'
	print '  -h, --help            Display this information'
	print '  -c, --conf=dir/file   Use this config file'
	print ('                        (default: %s)' %
		synctool.param.DEFAULT_CONF)
	print '''  -d, --diff=file       Show diff for file
  -1, --single=file     Update a single file
  -r, --ref=file        Show which source file synctool chooses
  -e, --erase-saved     Erase *.saved backup files
  -f, --fix             Perform updates (otherwise, do dry-run)
      --no-post         Do not run any .post scripts
  -F, --fullpath        Show full paths instead of shortened ones
  -T, --terse           Show terse, shortened paths
      --color           Use colored output (only for terse mode)
      --no-color        Do not color output
      --unix            Output actions as unix shell commands
  -v, --verbose         Be verbose
  -q, --quiet           Suppress informational startup messages
      --version         Print current version number

synctool can help you administer your cluster of machines
Note that synctool does a dry run unless you specify --fix
'''


def get_options():
	global SINGLE_FILES

	progname = os.path.basename(sys.argv[0])

	# check for dangerous common typo's on the command-line
	be_careful_with_getopt()

	try:
		opts, args = getopt.getopt(sys.argv[1:], 'hc:d:1:r:efFTvq',
			['help', 'conf=', 'diff=', 'single=', 'ref=',
			'erase-saved', 'fix', 'no-post', 'fullpath',
			'terse', 'color', 'no-color', 'masterlog', 'nodename=',
			'verbose', 'quiet', 'unix', 'version'])
	except getopt.error, (reason):
		print '%s: %s' % (progname, reason)
		usage()
		sys.exit(1)

	except getopt.GetoptError, (reason):
		print '%s: %s' % (progname, reason)
		usage()
		sys.exit(1)

	except:
		usage()
		sys.exit(1)

	if args != None and len(args) > 0:
		stderr('error: excessive arguments on command line')
		sys.exit(1)

	for opt, arg in opts:
		if opt in ('-h', '--help', '-?'):
			usage()
			sys.exit(1)

		if opt in ('-c', '--conf'):
			synctool.param.CONF_FILE = arg
			continue

		if opt == '--version':
			print synctool.param.VERSION
			sys.exit(0)

	# first read the config file
	synctool.config.read_config()
	check_cmd_config()

	if not synctool.param.TERSE:
		# giving --terse changes program behavior as early as
		# in the get_options() loop itself, so set it here already
		for opt, args in opts:
			if opt in ('-T', '--terse'):
				synctool.param.TERSE = True
				synctool.param.FULL_PATH = False
				continue

			if opt in ('-F', '--fullpath'):
				synctool.param.FULL_PATH = True
				continue

	# then go process all the other options
	errors = 0

	action = ACTION_DEFAULT
	SINGLE_FILES = []

	# these are only used for checking the validity of option combinations
	opt_diff = False
	opt_single = False
	opt_reference = False
	opt_erase_saved = False
	opt_upload = False
	opt_suffix = False
	opt_fix = False

	for opt, arg in opts:
		if opt in ('-h', '--help', '-?', '-c', '--conf', '-T', '--terse',
					'-F', '--fullpath', '--version'):
			# already done
			continue

		if opt in ('-f', '--fix'):
			opt_fix = True
			synctool.lib.DRY_RUN = False
			continue

		if opt == '--no-post':
			synctool.lib.NO_POST = True
			continue

		if opt == '--color':
			synctool.param.COLORIZE = True
			continue

		if opt == '--no-color':
			synctool.param.COLORIZE = False
			continue

		if opt in ('-v', '--verbose'):
			synctool.lib.VERBOSE = True
			continue

		if opt in ('-q', '--quiet'):
			synctool.lib.QUIET = True
			continue

		if opt == '--unix':
			synctool.lib.UNIX_CMD = True
			continue

		if opt == '--masterlog':
			# used by the master for message logging purposes
			synctool.lib.MASTERLOG = True
			continue

		if opt == '--nodename':
			# used by the master to set the client's nodename
			synctool.param.NODENAME = arg
			continue

		if opt in ('-d', '--diff'):
			opt_diff = True
			action = ACTION_DIFF
			f = synctool.lib.strip_path(arg)
			if not f:
				stderr('missing filename')
				sys.exit(1)

			if f[0] != '/':
				stderr('please supply a full destination path')
				sys.exit(1)

			if not f in SINGLE_FILES:
				SINGLE_FILES.append(f)
			continue

		if opt in ('-1', '--single'):
			opt_single = True
			f = synctool.lib.strip_path(arg)
			if not f:
				stderr('missing filename')
				sys.exit(1)

			if f[0] != '/':
				stderr('please supply a full destination path')
				sys.exit(1)

			if not f in SINGLE_FILES:
				SINGLE_FILES.append(f)
			continue

		if opt in ('-r', '--ref', '--reference'):
			opt_reference = True
			action = ACTION_REFERENCE
			f = synctool.lib.strip_path(arg)
			if not f:
				stderr('missing filename')
				sys.exit(1)

			if f[0] != '/':
				stderr('please supply a full destination path')
				sys.exit(1)

			if not f in SINGLE_FILES:
				SINGLE_FILES.append(f)
			continue

		if opt in ('-e', '--erase-saved'):
			opt_erase_saved = True
			action = ACTION_ERASE_SAVED
			continue

		stderr("unknown command line option '%s'" % opt)
		errors += 1

	if errors:
		usage()
		sys.exit(1)

	option_combinations(opt_diff, opt_single, opt_reference, opt_erase_saved,
		opt_upload, opt_suffix, opt_fix)

	return action


def main():
	synctool.param.init()

	action = get_options()

	synctool.config.init_mynodename()

	if not synctool.param.NODENAME:
		stderr('unable to determine my nodename (%s)' %
				synctool.param.HOSTNAME)
		stderr('please check %s' % synctool.param.CONF_FILE)
		sys.exit(-1)

	if not synctool.param.NODES.has_key(synctool.param.NODENAME):
		stderr("unknown node '%s'" % synctool.param.NODENAME)
		stderr('please check %s' % synctool.param.CONF_FILE)
		sys.exit(-1)

	if synctool.param.NODENAME in synctool.param.IGNORE_GROUPS:
		# this is only a warning ...
		# you can still run synctool-pkg on the client by hand
		stderr('warning: node %s is disabled in %s' %
			(synctool.param.NODENAME, synctool.param.CONF_FILE))

	if synctool.lib.UNIX_CMD:
		t = time.localtime(time.time())

		unix_out('#')
		unix_out('# script generated by synctool on '
				'%04d/%02d/%02d %02d:%02d:%02d' %
				(t[0], t[1], t[2], t[3], t[4], t[5]))
		unix_out('#')
		unix_out('# NODENAME=%s' % synctool.param.NODENAME)
		unix_out('# HOSTNAME=%s' % synctool.param.HOSTNAME)
		unix_out('# ROOTDIR=%s' % synctool.param.ROOTDIR)
		unix_out('#')

		if not synctool.lib.DRY_RUN:
			unix_out('# NOTE: --fix specified, applying updates')
			unix_out('#')

		unix_out('')
	else:
		if not synctool.lib.MASTERLOG:
			# only print this when running stand-alone
			if not synctool.lib.QUIET:
				if synctool.lib.DRY_RUN:
					stdout('DRY RUN, not doing any updates')
					terse(synctool.lib.TERSE_DRYRUN, 'not doing any updates')
				else:
					stdout('--fix specified, applying changes')
					terse(synctool.lib.TERSE_FIXING, ' applying changes')

			else:
				if synctool.lib.DRY_RUN:
					verbose('DRY RUN, not doing any updates')
				else:
					verbose('--fix specified, applying changes')

		verbose('my nodename: %s' % synctool.param.NODENAME)
		verbose('my hostname: %s' % synctool.param.HOSTNAME)
		verbose('rootdir: %s' % synctool.param.ROOTDIR)

	os.environ['SYNCTOOL_NODE'] = synctool.param.NODENAME
	os.environ['SYNCTOOL_ROOT'] = synctool.param.ROOTDIR

	unix_out('umask 077')
	unix_out('')
	os.umask(077)

	if action == ACTION_DIFF:
		diff_files()

	elif action == ACTION_REFERENCE:
		reference_files()

	elif action == ACTION_ERASE_SAVED:
		if len(SINGLE_FILES) > 0:
			single_erase_saved()
		else:
			erase_saved()

	elif len(SINGLE_FILES) > 0:
		single_files()

	else:
		overlay_files()
		delete_files()

	unix_out('# EOB')


if __name__ == '__main__':
	try:
		main()
	except IOError, ioerr:
		if ioerr.errno == errno.EPIPE:		# Broken pipe
			pass
		else:
			print ioerr

	except KeyboardInterrupt:		# user pressed Ctrl-C
		print

# EOB
