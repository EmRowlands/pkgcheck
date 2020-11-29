import os
import subprocess
import tempfile
import textwrap

import pytest
from pkgcheck.scripts import pkgcheck
from pkgcheck.reporters import StrReporter
from pkgcheck.results import Result
from pkgcore import const as pkgcore_const
from pkgcore.ebuild import cpv as cpv_mod
from pkgcore.ebuild import repository
from pkgcore.util.commandline import Tool
from snakeoil import klass
from snakeoil.formatters import PlainTextFormatter
from snakeoil.fileutils import touch
from snakeoil.osutils import pjoin


def pytest_assertrepr_compare(op, left, right):
    """Custom assertion failure output."""
    if isinstance(left, Result) and isinstance(right, Result) and op == "==":
        with tempfile.TemporaryFile() as f:
            with StrReporter(out=PlainTextFormatter(f)) as reporter:
                reporter.report(left)
                reporter.report(right)
                f.seek(0)
                left_val, right_val = f.read().decode().splitlines()
        return ["Result instances !=:", left_val, right_val]


@pytest.fixture(scope="session")
def fakeconfig(tmp_path_factory):
    """Generate a portage config that sets the default repo to pkgcore's stubrepo."""
    fakeconfig = tmp_path_factory.mktemp('fakeconfig')
    repos_conf = fakeconfig / 'repos.conf'
    stubrepo = pjoin(pkgcore_const.DATA_PATH, 'stubrepo')
    with open(repos_conf, 'w') as f:
        f.write(textwrap.dedent(f"""\
            [DEFAULT]
            main-repo = stubrepo

            [stubrepo]
            location = {stubrepo}
        """))
    return str(fakeconfig)


@pytest.fixture(scope="session")
def testconfig(tmp_path_factory):
    """Generate a portage config that sets the default repo to pkgcore's stubrepo.

    Also, repo entries for all the bundled test repos.
    """
    testconfig = tmp_path_factory.mktemp('testconfig')
    repos_conf = testconfig / 'repos.conf'
    stubrepo = pjoin(pkgcore_const.DATA_PATH, 'stubrepo')
    testdir = pjoin(os.path.dirname(os.path.dirname(__file__)), 'repos')
    with open(repos_conf, 'w') as f:
        f.write(textwrap.dedent(f"""\
            [DEFAULT]
            main-repo = stubrepo

            [stubrepo]
            location = {stubrepo}
            [overlayed]
            location = {pjoin(testdir, 'overlayed')}
        """))
    return str(testconfig)


@pytest.fixture(scope="session")
def cache_dir(tmp_path_factory):
    """Generate a cache directory for pkgcheck."""
    cache_dir = tmp_path_factory.mktemp('cache')
    return str(cache_dir)


@pytest.fixture
def fakerepo(tmp_path_factory):
    """Generate a stub repo."""
    fakerepo = str(tmp_path_factory.mktemp('fakerepo'))
    os.makedirs(pjoin(fakerepo, 'profiles'))
    os.makedirs(pjoin(fakerepo, 'metadata'))
    with open(pjoin(fakerepo, 'profiles', 'repo_name'), 'w') as f:
        f.write('fakerepo\n')
    with open(pjoin(fakerepo, 'metadata', 'layout.conf'), 'w') as f:
        f.write('masters =\n')
    return fakerepo


@pytest.fixture(scope="session")
def tool(fakeconfig):
    """Generate a tool utility for running pkgcheck."""
    tool = Tool(pkgcheck.argparser)
    tool.parser.set_defaults(override_config=fakeconfig)
    return tool


class GitRepo:
    """Class for creating/manipulating git repos.

    Only relies on the git binary existing in order to limit
    dependency requirements.
    """

    def __init__(self, path, commit=False):
        self.path = path
        # initialize the repo
        self.run(['git', 'init'])
        self.run(['git', 'config', 'user.email', 'person@email.com'])
        self.run(['git', 'config', 'user.name', 'Person'])
        if commit:
            if self.changes:
                # if files exist in the repo, add them in an initial commit
                self.add_all(msg='initial commit')
            else:
                # otherwise add a stub initial commit
                self.add(pjoin(self.path, '.init'), create=True)

    def run(self, cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs):
        return subprocess.run(
            cmd, cwd=self.path, encoding='utf8', check=True,
            stdout=stdout, stderr=stderr, **kwargs)

    @property
    def changes(self):
        """Return a list of any untracked or modified files in the repo."""
        cmd = ['git', 'ls-files', '-mo', '--exclude-standard']
        p = self.run(cmd, stdout=subprocess.PIPE)
        return p.stdout.splitlines()

    @property
    def HEAD(self):
        """Return the commit hash for git HEAD."""
        p = self.run(['git', 'rev-parse', '--short', 'HEAD'], stdout=subprocess.PIPE)
        return p.stdout.strip()

    def __str__(self):
        return self.path

    def add(self, file_path, msg='commit', create=False):
        """Add a file and commit it to the repo."""
        if create:
            touch(pjoin(self.path, file_path))
        self.run(['git', 'add', file_path])
        self.run(['git', 'commit', '-m', msg])

    def add_all(self, msg='commit-all'):
        """Add and commit all tracked and untracked files."""
        self.run(['git', 'add', '--all'])
        self.run(['git', 'commit', '-m', msg])

    def remove(self, path, msg='remove'):
        """Remove a given file path and commit the change."""
        self.run(['git', 'rm', path])
        self.run(['git', 'commit', '-m', msg])

    def remove_all(self, path, msg='remove-all'):
        """Remove all files from a given path and commit the changes."""
        self.run(['git', 'rm', '-rf', path])
        self.run(['git', 'commit', '-m', msg])

    def move(self, path, new_path, msg=None):
        """Move a given file path and commit the change."""
        msg = msg if msg is not None else f'{path} -> {new_path}'
        self.run(['git', 'mv', path, new_path])
        self.run(['git', 'commit', '-m', msg])


@pytest.fixture
def git_repo(tmp_path_factory):
    """Create an empty git repo with an initial commit."""
    return GitRepo(str(tmp_path_factory.mktemp('git-repo')), commit=True)


@pytest.fixture
def make_git_repo(tmp_path_factory):
    """Factory for git repo creation."""
    def _make_git_repo(path=None, **kwargs):
        path = str(tmp_path_factory.mktemp('git-repo')) if path is None else path
        return GitRepo(path, **kwargs)
    return _make_git_repo


class EbuildRepo:
    """Class for creating/manipulating ebuild repos."""

    def __init__(self, path, repo_id='fake', masters=(), arches=()):
        self.path = path
        try:
            os.makedirs(pjoin(path, 'profiles'))
            with open(pjoin(path, 'profiles', 'repo_name'), 'w') as f:
                f.write(f'{repo_id}\n')
            os.makedirs(pjoin(path, 'metadata'))
            with open(pjoin(path, 'metadata', 'layout.conf'), 'w') as f:
                f.write(textwrap.dedent(f"""\
                    masters = {' '.join(masters)}
                    cache-formats =
                    thin-manifests = true
                """))
            with open(pjoin(path, 'profiles', 'arch.list'), 'w') as f:
                f.write('\n'.join(arches))
            # create a fake 'blank' license
            os.makedirs(pjoin(path, 'licenses'))
            touch(pjoin(path, 'licenses', 'blank'))
        except FileExistsError:
            pass
        self._repo = repository.UnconfiguredTree(path)

    def create_ebuild(self, cpvstr, data=None, **kwargs):
        cpv = cpv_mod.VersionedCPV(cpvstr)
        ebuild_dir = pjoin(self.path, cpv.category, cpv.package)
        os.makedirs(ebuild_dir, exist_ok=True)

        # use defaults for some ebuild metadata if unset
        eapi = kwargs.pop('eapi', '7')
        slot = kwargs.pop('slot', '0')
        desc = kwargs.pop('description', 'stub package description')
        homepage = kwargs.pop('homepage', 'https://github.com/pkgcore/pkgcheck')
        license = kwargs.pop('license', 'blank')

        with open(pjoin(ebuild_dir, f'{cpv.package}-{cpv.version}.ebuild'), 'w') as f:
            if self.repo_id == 'gentoo':
                f.write(textwrap.dedent("""\
                    # Copyright 1999-2020 Gentoo Authors
                    # Distributed under the terms of the GNU General Public License v2
                """))
            f.write(f'EAPI="{eapi}"\n')
            f.write(f'DESCRIPTION="{desc}"\n')
            f.write(f'HOMEPAGE="{homepage}"\n')
            f.write(f'SLOT="{slot}"\n')
            f.write(f'LICENSE="{license}"\n')
            for k, v in kwargs.items():
                # handle sequences such as KEYWORDS and IUSE
                if isinstance(v, (tuple, list)):
                    v = ' '.join(v)
                f.write(f'{k.upper()}="{v}"\n')
            if data is not None:
                f.write(data)

    def __iter__(self):
        yield from iter(self._repo)

    __getattr__ = klass.GetAttrProxy('_repo')
    __dir__ = klass.DirProxy('_repo')


@pytest.fixture
def repo(tmp_path_factory):
    """Create a generic ebuild repository."""
    return EbuildRepo(str(tmp_path_factory.mktemp('repo')))


@pytest.fixture
def make_repo(tmp_path_factory):
    """Factory for ebuild repo creation."""
    def _make_repo(path=None, **kwargs):
        path = str(tmp_path_factory.mktemp('repo')) if path is None else path
        return EbuildRepo(path, **kwargs)
    return _make_repo
