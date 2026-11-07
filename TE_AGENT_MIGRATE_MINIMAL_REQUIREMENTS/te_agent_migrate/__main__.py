import os
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = [sys.executable, '-m', 'pip', 'install', '--target', os.path.join(PROJECT_DIR, 'libs'),
           '-r', os.path.join(PROJECT_DIR, 'requirements.txt')]


def _install_libraries():
    """The two libraries are missing: offer to copy them into the libs folder, then start again."""
    print('The libraries this tool needs (requests, urllib3) are not installed.')
    print('They are copied into the folder "libs" inside this project. Nothing is installed into your Python.')
    command = ' '.join('"%s"' % part if ' ' in part else part for part in INSTALL)
    if not sys.stdin.isatty():
        print('Run this once, then start the tool again:\n  %s' % command)
        return 2
    if input('Install them now? [Y/n]: ').strip().lower() not in ('', 'y', 'yes'):
        print('Run this once, then start the tool again:\n  %s' % command)
        return 2
    if subprocess.call(INSTALL) != 0:
        print('The installation failed. Check the network access to pypi.org and try again.')
        return 2
    os.execv(sys.executable, [sys.executable, '-m', 'te_agent_migrate'] + sys.argv[1:])


def run():
    if sys.version_info < (3, 6):
        print('This tool needs Python 3.6 or newer; this is %s.' % sys.version.split()[0])
        return 2
    try:
        from te_agent_migrate.cli import main
    except ImportError as exc:
        if getattr(exc, 'name', '') in ('requests', 'urllib3') or 'requests' in str(exc) or 'urllib3' in str(exc):
            return _install_libraries()
        raise
    return main()


if __name__ == '__main__':
    sys.exit(run())
