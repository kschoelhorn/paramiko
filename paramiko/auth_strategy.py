from collections import namedtuple

from .agent import AgentKey
from .util import get_logger
from .ssh_exception import AuthenticationException


class AuthSource:
    """
    Some SSH authentication source, such as a password, private key, or agent.

    See subclasses in this module for concrete implementations.

    All implementations must accept at least a ``username`` (``str``) kwarg.
    """

    def __init__(self, username):
        self.username = username

    def _repr(self, **kwargs):
        # TODO: are there any good libs for this? maybe some helper from
        # structlog?
        pairs = [f"{k}={v!r}" for k, v in kwargs.items()]
        joined = ", ".join(pairs)
        return f"{self.__class__.__name__}({joined})"

    def __repr__(self):
        return self._repr()

    def authenticate(self, transport):
        """
        Perform authentication.
        """
        raise NotImplementedError


def NoneAuth(AuthSource):
    """
    Auth type "none", ie https://www.rfc-editor.org/rfc/rfc4252#section-5.2 .
    """

    def authenticate(self, transport):
        return transport.auth_none(self.username)


class Password(AuthSource):
    """
    Password authentication.

    :param callable password_getter:
        A lazy callable that should return a `str` password value at
        authentication time, such as a `functools.partial` wrapping
        `getpass.getpass`, an API call to a secrets store, or similar.

        If you already know the password at instantiation time, you should
        simply use something like ``lambda: "my literal"`` (for a literal, but
        also, shame on you!) or ``lambda: variable_name (for something stored
        in a variable).
    """

    def __init__(self, username, password_getter):
        super().__init__(username=username)
        self.password_getter = password_getter

    def __repr__(self):
        # Password auth is marginally more 'username-caring' than pkeys, so may
        # as well log that info here.
        return super()._repr(user=self.username)

    def authenticate(self, transport):
        # Lazily get the password, in case it's prompting a user
        password = self.password_getter()
        return transport.auth_password(self.username, password)


class PrivateKey(AuthSource):
    """
    Essentially a mixin for private keys.

    Knows how to auth, but leaves key material discovery/loading/decryption to
    subclasses.

    Subclasses **must** ensure that they've set ``self.pkey`` to a decrypted
    `.PKey` instance before calling ``super().authenticate``; typically
    either in their ``__init__``, or in an overridden ``authenticate`` prior to
    its `super` call.
    """

    def authenticate(self, transport):
        return transport.auth_publickey(self.username, self.pkey)


class InMemoryPrivateKey(PrivateKey):
    """
    An in-memory, decrypted `.PKey` object.
    """

    def __init__(self, username, pkey):
        super().__init__(username=username)
        # No decryption (presumably) necessary!
        self.pkey = pkey

    def __repr__(self):
        # NOTE: most of interesting repr-bits for private keys is in PKey.
        # TODO: tacking on agent-ness like this is a bit awkward, but, eh?
        rep = super()._repr(pkey=self.pkey)
        if isinstance(self.pkey, AgentKey):
            rep += " [agent]"
        return rep


class OnDiskPrivateKey(PrivateKey):
    """
    Some on-disk private key that needs opening and possibly decrypting.

    :param str source:
        String tracking where this key's path was specified; should be one of
        ``"ssh-config"``, ``"python-config"``, or ``"implicit-home"``.
    :param Path path:
        The filesystem path this key was loaded from.
    :param PKey pkey:
        The `PKey` object this auth source uses/represents.
    """

    # TODO: how to log/note how this path came to our attention (ssh_config,
    # fabric config, some direct kwarg somewhere, CLI flag, etc)? Different
    # subclasses for all of those seems like massive overkill, so just some
    # sort of "via" or "source" string argument?
    def __init__(self, username, source, path, pkey):
        super().__init__(username=username)
        self.source = source
        self.path = path
        # Superclass wants .pkey, other two are mostly for display/debugging.
        self.pkey = pkey

    def __repr__(self):
        return self._repr(
            key=self.pkey, source=self.source, path=str(self.path)
        )


SourceResult = namedtuple("SourceResult", ["source", "result"])

class AuthResult(list):
    """
    Represents a partial or complete SSH authentication attempt.

    This class conceptually extends `AuthStrategy` by pairing the former's
    authentication **sources** with the **results** of trying to authenticate
    with them.

    `AuthResult` is a (subclass of) `list` of `namedtuple`, which are of the
    form ``namedtuple('SourceResult', 'source', 'result')`` (where the
    ``source`` member is an `AuthSource` and the ``result`` member is either a
    return value from the relevant `.Transport` method, or an exception
    object).

    Instances also have a `strategy` attribute referencing the `AuthStrategy`
    which was attempted.
    """

    def __init__(self, strategy, *args, **kwargs):
        self.strategy = strategy
        super().__init__(*args, **kwargs)

    def __str__(self):
        # NOTE: meaningfully distinct from __repr__, which still wants to use
        # superclass' implementation.
        return "\n".join(f"{x.source} -> {x.result}" for x in self)


# TODO 4.0: descend from SSHException or even just Exception
class AuthFailure(AuthenticationException):
    """
    Basic exception wrapping an `AuthResult` indicating overall auth failure.

    Note that `AuthFailure` descends from `AuthenticationException` but is
    generally "higher level"; the latter is now only raised by individual
    `AuthSource` attempts and should typically only be seen by users when
    encapsulated in this class. It subclasses `AuthenticationException`
    primarily for backwards compatibility reasons.
    """

    def __init__(self, result):
        self.result = result

    def __str__(self):
        return "\n" + str(self.result)


class AuthStrategy:
    """
    This class represents one or more attempts to auth with an SSH server.

    By default, subclasses must at least accept an ``ssh_config``
    (`.SSHConfig`) keyword argument, but may opt to accept more as needed for
    their particular strategy.
    """

    def __init__(
        self,
        ssh_config,
    ):
        self.ssh_config = ssh_config
        self.log = get_logger(__name__)

    def get_sources(self):
        """
        Generator yielding `AuthSource` instances, in the order to try.

        This is the primary override point for subclasses: you figure out what
        sources you need, and ``yield`` them.

        Subclasses _of_ subclasses may find themselves wanting to do things
        like filtering or discarding around a call to `super`.
        """
        raise NotImplementedError

    def authenticate(self, transport):
        """
        Handles attempting `AuthSource` instances yielded from `get_sources`.

        You *normally* won't need to override this, but it's an option for
        advanced users.
        """
        succeeded = False
        overall_result = AuthResult(strategy=self)
        for source in self.get_sources(transport):
            self.log.debug(f"Trying {source}")
            try:  # NOTE: this really wants to _only_ wrap the authenticate()!
                result = source.authenticate(transport)
                succeeded = True
            except Exception as e:
                result = e
                # NOTE: showing type, not message, for tersity & also most of
                # the time it's basically just "Authentication failed."
                source_class = e.__class__.__name__
                self.log.info(
                    f"Authentication via {source} failed with {source_class}"
                )
            overall_result.append(SourceResult(source, result))
            if succeeded:
                break
        # Gotta die here if nothing worked, otherwise Transport's main loop
        # just kinda hangs out until something times out!
        if not succeeded:
            raise AuthFailure(result=overall_result)
        # Success: give back what was done, in case they care.
        return overall_result