from contextlib import contextmanager
from importlib import import_module
from time import time
import copy
import logging
import os

import pygit2

from gitmodel import conf
from gitmodel import exceptions
from gitmodel import fields
from gitmodel import models
from gitmodel.utils import git


class Workspace(object):
    """
    A workspace acts as an encapsulation within which any model work is done.
    It is analogous to a git working directory. It also acts as a "porcelain"
    layer to pygit2's "plumbing".
    
    In contrast to a working directory, this class does not make use of the repository's 
    INDEX and HEAD files, and instead keeps track of the these in memory.

    Passing default_branch will 
    """
    def __init__(self, repo_path, initial_branch='refs/heads/master'):
        self.config = conf.Config()

        # set up a model registry 
        class ModelRegistry(dict):
            """This class acts like a so-called AttrDict"""
            def __init__(self):
                self.__dict__ = self

        self.models = ModelRegistry()

        try:
            self.repo = pygit2.Repository(repo_path)
        except KeyError:
            msg = "Git repository not found at {}".format(repo_path)
            raise exceptions.RepositoryNotFound(msg)

        self.index = None
        
        # set default head
        self.head = initial_branch
         
        # Set branch to head. If it the branch (head commit) doesn't exist, set
        # index to a new empty tree.
        try:
            self.repo.lookup_reference(self.head)
        except KeyError:
            oid = self.repo.TreeBuilder().write()
            self.index = self.repo[oid]
        else:
            self.update_index(self.head)

        # add a base GitModel which can be extended if needed
        self.register_model(models.GitModelBase, 'GitModel')

        self.log = logging.getLogger(__name__)

    def register_model(self, cls, name=None):
        """
        Register a GitModelBase class with this workspace. Only classes that do
        not extend GitModel (ie, have not yet been metaclassed) can be
        registered. Registering with the workspace will create the appropriate
        metaclass and will store the resulting GitModel instance in the
        workspace's ``models`` attribute.
        """
        if not name:
            name = cls.__name__

        if self.models.get(name):
            return self.models[name]

        if isinstance(cls, models.DeclarativeMetaclass):
            msg = "{0} is already registered with a workspace"
            raise ValueError(msg.format(cls.__name__))

        metaclass = models.DeclarativeMetaclass
        attrs = dict(cls.__dict__, **{
            '__workspace__': self,
            '__module__': __name__,
        })
        if attrs.get('__dict__'):
            del attrs['__dict__']

        # parents must also be initialized with the metaclass
        bases = []
        for base in cls.__bases__:
            if not isinstance(base, models.DeclarativeMetaclass) \
                    and issubclass(base, models.GitModelBase) \
                    and base != models.GitModelBase:
                base = self.models.get(name) or self.register_model(base)
            bases.append(base)

        # any related fields need to be updated so that they point to the
        # correct model on the workspace
        for attname, attr in attrs.iteritems():
            if not isinstance(attr, fields.RelatedField):
                continue

            # make a shallow copy of the field and "reset" it
            attrs[attname] = copy.copy(attr)
            attrs[attname].model = None

            # if the target model already has a workspace don't update it
            if issubclass(attr.to_model, models.DeclarativeMetaclass):
                continue

            # register to_model on this workspace
            to_model = self.models.get(attr.to_model.__name__) or \
                self.register_model(attr.to_model)
            attrs[attname].to_model = to_model

        # create the new class and attach it to the workspace
        new_model = metaclass(name, tuple(bases), attrs)
        self.models[name] = new_model
        return new_model

    def import_models(self, path_or_module):
        """
        Register all models declared within a given python module
        """
        if isinstance(path_or_module, basestring):
            mod = import_module(path_or_module)
        else:
            mod = path_or_module

        for name in dir(mod):
            item = getattr(mod, name)
            if isinstance(item, type) and issubclass(item, models.GitModelBase):
                self.register_model(item, name)

    def create_blob(self, content):
        return self.repo.create_blob(content)

    def create_branch(self, name, start_point=None):
        """
        Creates a head reference with the given name. The start_point argument
        is the head to which the new branch will point -- it may be a branch 
        name, commit id, or tag name (defaults to current branch).
        """
        if not start_point:
            start_point = self.head
        try:
            start_point_ref = self.repo.lookup_reference(start_point)
        except KeyError:
            raise exceptions.RepositoryError("Reference not found: {}".format(start_point))

        if start_point_ref.type != pygit2.GIT_OBJ_COMMIT:
            raise ValueError('Given reference must point to a commit')
        branch_ref  = 'refs/heads/{}'.format(name)
        self.repo.create_reference(branch_ref, start_point_ref.target)

    def set_branch(self, name):
        """
        Sets the current head ref to the given branch name
        """
        ref  = 'refs/heads/{}'.format(name)
        try:
            self.repo.lookup_reference(ref)
        except KeyError:
            raise exceptions.RepositoryError("Reference not found: {}".format(ref))
        self.update_index(ref)

    @property
    def branch(self):
        #FIXME: this is expensive (and dumb).
        try:
            return Branch(self.repo, self.head)
        except KeyError:
            return None

    def update_index(self, ref=None):
        """Sets the index to the current branch or to the given ref"""
        # Don't change the index if there are pending changes.
        if self.index and self.has_changes():
            msg = "Cannot checkout a different branch with pending changes"
            raise exceptions.RepositoryError(msg)
        try:
            self.repo.lookup_reference(ref)
        except KeyError:
            raise exceptions.RepositoryError("Reference not found: {}".format(ref))
        self.head = ref
        self.index = self.branch.tree

    def add(self, path, entries):
        """
        Updates the current index given a path and a list of entries
        """
        oid = git.build_path(self.repo, path, entries, self.index)
        self.index = self.repo[oid]

    def add_blob(self, path, content, mode=pygit2.GIT_FILEMODE_BLOB):
        """
        Creates a blob object and adds it to the current index
        """
        path, name = os.path.split(path)
        blob = self.repo.create_blob(content)
        entry = (name, blob, mode)
        self.add(path, [entry])
        return blob

    @contextmanager
    def commit_on_success(self, message='', author=None, committer=None):
        """
        A context manager that allows you to wrap a block of changes and 
        commit those changes if no exceptions occur. This also ensures that
        the repository is in a clean state (i.e., no changes) before allowing
        any further changes.
        """
        # ensure a clean state
        if self.has_changes():
            msg = "Repository has pending changes. Cannot auto-commit until "\
                  "pending changes have been comitted."
            raise exceptions.RepositoryError(msg)

        yield

        self.commit(message, author, committer)
    
    def diff(self):
        """
        Returns a pygit2.Diff object representing a diff between the current
        index and the current branch.
        """
        if self.branch:
            tree = self.branch.tree
        else:
            empty_tree = self.repo.TreeBuilder().write()
            tree = self.repo[empty_tree]
        return tree.diff_to_tree(self.index)

    def has_changes(self):
        """Returns True if the current tree differs from the current branch"""
        # As of pygit2 0.19, Diff.patch seems to raise a non-descript GitError
        # if there are  no changes, so we check the iterable length instead.
        return len(tuple(self.diff())) > 0
    
    def commit(self, message='', author=None, committer=None):
        """Commits the current tree to the current branch."""
        if not self.has_changes():
            return None
        parents = []
        if self.branch:
            parents = [self.branch.commit.oid]
        return self.create_commit(self.head, self.index, message, author, committer, parents)
       
    def create_commit(self, ref, tree, message='', author=None, committer=None, parents=None):
        """
        Create a commit with the given ref, tree, and message. If parent
        commits are not given, the commit pointed to by the given ref is used
        as the parent. If author and commitor are not given, the defaults in
        the config are used.
        """
        if not author:
            author = self.config.DEFAULT_GIT_USER
        if not committer:
            committer = author
        
        default_offset = self.config.get('DEFAULT_TZ_OFFSET', None)
        author = git.make_signature(*author, default_offset=default_offset)
        committer = git.make_signature(*committer, default_offset=default_offset)

        if parents is None:
            try:
                parent_ref = self.repo.lookup_reference(ref)
            except KeyError:
                parents = [] #initial commit
            else:
                parents = [parent_ref.oid]
        
        # FIXME: create_commit updates the HEAD ref. This may lead to race
        # conditions. As long as HEAD isn't used for anything in the system, it
        # shouldn't be a problem.
        return self.repo.create_commit(ref, author, committer, message, tree.oid, parents)
    
    def walk(self, sort=pygit2.GIT_SORT_TIME):
        """Iterate through commits on the current branch"""
        #NEEDS-TEST
        for commit in self.repo.walk(self.branch.oid, sort):
            yield commit

    @contextmanager
    def lock(self, id):
        """
        Acquires a lock with the given id. Uses an empty reference to store the
        lock state, eg: refs/locks/my-lock
        """
        start_time = time()
        while self.locked(id):
            if time() - start_time > self.config.LOCK_WAIT_TIMEOUT:
                msg = "Lock wait timeout exceeded while trying to acquire lock '{}' on {}"
                msg = msg.format(id, self.path)
                raise exceptions.LockWaitTimeoutExceeded(msg)
            time.sleep(self.config.LOCK_WAIT_INTERVAL)

        # The blob itself is not important, just the fact that the ref exists
        emptyblob = self.create_blob('')
        ref = self.create_reference('refs/locks/{}'.format(id), emptyblob)
        yield
        ref.delete()

    def locked(self, id):
        try:
            self.repo.lookup_reference('refs/locks/{}'.format(id))
        except KeyError:
            return False
        return True

class Branch(object):
    """
    A representation of a git branch that provides quick access to the ref,
    commit, and commit tree.
    """
    def __init__(self, repo, ref):
        self.ref = repo.lookup_reference(ref)
        self.commit = self.ref.get_object()
        self.oid = self.commit.oid
        self.tree = self.commit.tree
