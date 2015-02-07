# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools
import hmac
import time

import msgpack
import msgpack.exceptions
import redis

from pyramid.interfaces import ISession, ISessionFactory
from pyramid.tweens import MAIN
from zope.interface import implementer

from warehouse.utils import crypto
from warehouse.utils.http import add_vary


def uses_session(view):
    @functools.wraps(view)
    def wrapped(context, request):
        # We want to restore the session object to the request.session location
        # because this view is allowed to use the session.
        request.session = request._session

        # Call our view with our now modified request.
        return view(context, request)

    # Wrap our already wrapped view with another wrapper which will ensure that
    # there is a Vary: Cookie header applied.
    wrapped = add_vary("Cookie")(wrapped)

    return wrapped


def session_tween_factory(handler, registry):
    def session_tween(request):
        # Stash our real session object in a private location on the request so
        # we can access it later.
        request._session = request.session

        # Set our request.session to an InvalidSession() which will raise
        # errors anytime someone attempts to use it.
        request.session = InvalidSession()

        # Call our handler with the request, and no matter what ensure that
        # after we've called it that the request.session has been set back to
        # it's real value.
        try:
            return handler(request)
        finally:
            request.session = request._session

    return session_tween


def _changed_method(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        self.changed()
        return method(self, *args, **kwargs)
    return wrapped


def _invalid_method(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        self._error_message()
    return wrapped


@implementer(ISession)
class InvalidSession(dict):

    __contains__ = _invalid_method(dict.__contains__)
    __delitem__ = _invalid_method(dict.__delitem__)
    __getitem__ = _invalid_method(dict.__getitem__)
    __iter__ = _invalid_method(dict.__iter__)
    __len__ = _invalid_method(dict.__len__)
    __setitem__ = _invalid_method(dict.__setitem__)
    clear = _invalid_method(dict.clear)
    copy = _invalid_method(dict.copy)
    fromkeys = _invalid_method(dict.fromkeys)
    get = _invalid_method(dict.get)
    items = _invalid_method(dict.items)
    keys = _invalid_method(dict.keys)
    pop = _invalid_method(dict.pop)
    popitem = _invalid_method(dict.popitem)
    setdefault = _invalid_method(dict.setdefault)
    update = _invalid_method(dict.update)
    values = _invalid_method(dict.values)

    def _error_message(self):
        raise RuntimeError(
            "Cannot use request.session in a view without @uses_session."
        )

    def __getattr__(self, name):
        self._error_message()


@implementer(ISession)
class Session(dict):

    _csrf_token_key = "_csrf_token"
    _flash_key = "_flash_messages"

    # A number of our methods need to be decorated so that they also call
    # self.changed()
    __delitem__ = _changed_method(dict.__delitem__)
    __setitem__ = _changed_method(dict.__setitem__)
    clear = _changed_method(dict.clear)
    pop = _changed_method(dict.pop)
    popitem = _changed_method(dict.popitem)
    setdefault = _changed_method(dict.setdefault)
    update = _changed_method(dict.update)

    def __init__(self, data=None, session_id=None, new=True):
        # Brand new sessions don't have any data, so we'll just create an empty
        # dictionary for them.
        if data is None:
            data = {}

        # Initialize our actual dictionary here.
        super().__init__(data)

        # We need to track the state of our Session.
        self._sid = session_id
        self._changed = False
        self.new = new
        self.created = int(time.time())
        self.invalidated = False

    @property
    def sid(self):
        if self._sid is None:
            self._sid = crypto.random_token()
        return self._sid

    @sid.deleter
    def sid(self):
        self._sid = None

    def changed(self):
        self._changed = True

    def invalidate(self):
        self.clear()
        self.new = True
        self.created = int(time.time())
        self.invalidated = True
        self._changed = False

    def should_save(self):
        return self._changed

    # Flash Messages Methods
    def _get_flash_queue_key(self, queue):
        return ".".join(filter(None, [self._flash_key, queue]))

    def flash(self, msg, queue="", allow_duplicate=True):
        queue_key = self._get_flash_queue_key(queue)

        # If we're not allowing duplicates check if this message is already
        # in the queue, and if it is just return immediately.
        if not allow_duplicate and msg in self[queue_key]:
            return

        self.setdefault(queue_key, []).append(msg)

    def peek_flash(self, queue=""):
        return self.get(self._get_flash_queue_key(queue), [])

    def pop_flash(self, queue=""):
        queue_key = self._get_flash_queue_key(queue)
        messages = self.get(queue_key, [])
        self.pop(queue_key, None)
        return messages

    # CSRF Methods
    def new_csrf_token(self):
        self[self._csrf_token_key] = crypto.random_token()
        return self[self._csrf_token_key]

    def get_csrf_token(self):
        token = self.get(self._csrf_token_key)
        if token is None:
            token = self.new_csrf_token()
        return token

    def get_scoped_csrf_token(self, scope):
        # Here we want to do
        # HMAC_sha512(HMAC_sha512(unscoped_token, scope), session_id). This
        # will make it possible to have scope specific CSRF tokens which means
        # that a single scope token being leaked cannot be used for other
        # scopes.
        unscoped = self.get_csrf_token().encode("utf8")
        scope = scope.encode("utf8")
        scoped = hmac.new(unscoped, scope, "sha512").hexdigest().encode("utf8")
        return hmac.new(scoped, self.sid.encode("utf8"), "sha512").hexdigest()

    def has_csrf_token(self):
        return self._csrf_token_key in self


@implementer(ISessionFactory)
class SessionFactory:

    cookie_name = "session_id"
    max_age = 12 * 60 * 60  # 12 hours

    def __init__(self, secret, url):
        self.redis = redis.StrictRedis.from_url(url)
        self.signer = crypto.TimestampSigner(secret, salt="session")

    def __call__(self, request):
        return self._process_request(request)

    def _redis_key(self, session_id):
        return "warehouse/session/data/{}".format(session_id)

    def _process_request(self, request):
        # Register a callback with the request so we can save the session once
        # it's finished.
        request.add_response_callback(self._process_response)

        # Load our session ID from the request.
        session_id = request.cookies.get(self.cookie_name)

        # If we do not have a session ID then we'll just use a new empty
        # session.
        if session_id is None:
            return Session()

        # Check to make sure we have a valid session id
        try:
            session_id = self.signer.unsign(session_id, max_age=self.max_age)
            session_id = session_id.decode("utf8")
        except crypto.BadSignature:
            return Session()

        # Fetch the serialized data from redis
        bdata = self.redis.get(self._redis_key(session_id))

        # If the session didn't exist in redis, we'll give the user a new
        # session.
        if bdata is None:
            return Session()

        # De-serialize our session data
        try:
            data = msgpack.unpackb(bdata, encoding="utf8", use_list=True)
        except (msgpack.exceptions.UnpackException,
                msgpack.exceptions.ExtraData):
            # If the session data was invalid we'll give the user a new session
            return Session()

        # If we were able to load existing session data, load it into a
        # Session class
        session = Session(data, session_id)

        return session

    def _process_response(self, request, response):
        # Check to see if the session has been marked to be deleted, if it has
        # benn then we'll delete it, and tell our response to delete the
        # session cookie as well.
        if request.session.invalidated:
            self.redis.delete(self._redis_key(request.session.sid))
            del request.session.sid
            if not request.session.should_save():
                response.delete_cookie(self.cookie_name)

        # Check to see if the session has been marked to be saved, generally
        # this means that the session data has been modified and thus we need
        # to store the new data.
        if request.session.should_save():
            # Save our session in Redis
            self.redis.setex(
                self._redis_key(request.session.sid),
                self.max_age,
                msgpack.packb(
                    request.session,
                    encoding="utf8",
                    use_bin_type=True,
                ),
            )

            # Send our session cookie to the client
            response.set_cookie(
                self.cookie_name,
                self.signer.sign(request.session.sid.encode("utf8")),
                max_age=self.max_age,
                httponly=True,
                secure=request.scheme == "https",
            )


def includeme(config):
    config.set_session_factory(
        SessionFactory(
            config.registry["config"].sessions.secret,
            config.registry["config"].sessions.url,
        ),
    )
    config.add_tween("warehouse.sessions.session_tween_factory", over=MAIN)