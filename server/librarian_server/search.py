# -*- mode: python; coding: utf-8 -*-
# Copyright 2016-2017 the HERA Collaboration
# Licensed under the BSD License.

"""Searches of the database

This code will likely need a lot of expansion, but we'll start simple.

"""

from __future__ import absolute_import, division, print_function, unicode_literals

__all__ = str('''
compile_search
StandingOrder
queue_standing_order_copies
register_standing_order_checkin
''').split()

import datetime
import json
import logging
import os.path
import six
import time

from flask import Response, flash, redirect, render_template, request, url_for

from . import app, db
from .dbutil import NotNull
from .webutil import ServerError, json_api, login_required, optional_arg, required_arg


# The search parser. We save searches in a (hopefully) simple JSON format. The
# format is documented in `docs/Searching.md`. KEEP THE DOCS UPDATED!

class _AttributeTypes(object):
    string = 's'
    int = 'i'
    float = 'f'


AttributeTypes = _AttributeTypes()


class GenericSearchCompiler(object):
    """A simple singleton class that helps with compiling searches. The only state
    that we manage is the list of search clauses, which can be extended
    dynamically to support different types of attributes that searchable
    things possess.

    """

    def __init__(self):
        self.clauses = {
            'and': self._do_and,
            'or': self._do_or,
            'none-of': self._do_none_of,
            'always-true': self._do_always_true,
            'always-false': self._do_always_false,
        }

    def compile(self, search):
        """Compile a search that is specified as a JSON-like data structure.

        The `search` must be a dict, which is interpreted as a set of clauses
        that are ANDed logically.

        """
        if isinstance(search, dict):
            return self._compile_clause('and', search)

        raise ServerError('can\'t parse search: data must '
                          'be in dict format; got %s', search.__class__.__name__)

    def _compile_clause(self, name, payload):
        impl = self.clauses.get(name)
        if impl is None:
            raise ServerError('can\'t parse search: unrecognized clause %r' % name)
        return impl(name, payload)

    # Framework for doing searches on general attributes of database items.

    def _add_attributes(self, cls, attr_info):
        from functools import partial

        for attr_name, attr_type, attr_getter in attr_info:
            clause_name = attr_name.replace('_', '-')

            if attr_getter is None:
                attr_getter = partial(getattr, cls, attr_name)

            if attr_type == AttributeTypes.string:
                self.clauses[clause_name +
                             '-is-exactly'] = partial(self._do_str_is_exactly, attr_getter)
                self.clauses[clause_name + '-is-not'] = partial(self._do_str_is_not, attr_getter)
                self.clauses[clause_name + '-matches'] = partial(self._do_str_matches, attr_getter)
            elif attr_type == AttributeTypes.int:
                self.clauses[clause_name +
                             '-is-exactly'] = partial(self._do_int_is_exactly, attr_getter)
                self.clauses[clause_name + '-is-not'] = partial(self._do_int_is_not, attr_getter)
                self.clauses[clause_name +
                             '-greater-than'] = partial(self._do_num_greater_than, attr_getter)
                self.clauses[clause_name +
                             '-less-than'] = partial(self._do_num_less_than, attr_getter)
                self.clauses[clause_name +
                             '-in-range'] = partial(self._do_num_in_range, attr_getter)
                self.clauses[clause_name +
                             '-not-in-range'] = partial(self._do_num_not_in_range, attr_getter)
            elif attr_type == AttributeTypes.float:
                self.clauses[clause_name +
                             '-greater-than'] = partial(self._do_num_greater_than, attr_getter)
                self.clauses[clause_name +
                             '-less-than'] = partial(self._do_num_less_than, attr_getter)
                self.clauses[clause_name +
                             '-in-range'] = partial(self._do_num_in_range, attr_getter)
                self.clauses[clause_name +
                             '-not-in-range'] = partial(self._do_num_not_in_range, attr_getter)

    def _do_str_matches(self, attr_getter, clause_name, payload):
        if not isinstance(payload, unicode):
            raise ServerError('can\'t parse "%s" clause: contents must be text, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return attr_getter().like(payload)

    def _do_str_is_exactly(self, attr_getter, clause_name, payload):
        if not isinstance(payload, unicode):
            raise ServerError('can\'t parse "%s" clause: contents must be text, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return (attr_getter() == payload)

    def _do_str_is_not(self, attr_getter, clause_name, payload):
        if not isinstance(payload, unicode):
            raise ServerError('can\'t parse "%s" clause: contents must be text, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return (attr_getter() != payload)

    def _do_int_is_exactly(self, attr_getter, clause_name, payload):
        if not isinstance(payload, int):
            raise ServerError('can\'t parse "%s" clause: contents must be an integer, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return (attr_getter() == payload)

    def _do_int_is_not(self, attr_getter, clause_name, payload):
        if not isinstance(payload, int):
            raise ServerError('can\'t parse "%s" clause: contents must be an integer, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return (attr_getter() != payload)

    def _do_num_greater_than(self, attr_getter, clause_name, payload):
        if not isinstance(payload, (int, float)):
            raise ServerError('can\'t parse "%s" clause: contents must be numeric, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return (attr_getter() > payload)

    def _do_num_less_than(self, attr_getter, clause_name, payload):
        if not isinstance(payload, (int, float)):
            raise ServerError('can\'t parse "%s" clause: contents must be numeric, '
                              'but got %s', clause_name, payload.__class__.__name__)
        return (attr_getter() < payload)

    def _do_num_in_range(self, attr_getter, clause_name, payload):
        if (not isinstance(payload, list) or
            len(payload) != 2 or
            not isinstance(payload[0], (int, float)) or
                not isinstance(payload[1], (int, float))):
            raise ServerError('can\'t parse "%s" clause: contents must be a list of two numbers, '
                              'but got %s', clause_name, payload.__class__.__name__)

        v1, v2 = payload
        if v1 > v2:
            v1, v2 = v2, v1

        from sqlalchemy import and_
        value = attr_getter()
        return and_(value >= v1, value <= v2)

    def _do_num_not_in_range(self, attr_getter, clause_name, payload):
        if (not isinstance(payload, list) or
            len(payload) != 2 or
            not isinstance(payload[0], (int, float)) or
                not isinstance(payload[1], (int, float))):
            raise ServerError('can\'t parse "%s" clause: contents must be a list of two numbers, '
                              'but got %s', clause_name, payload.__class__.__name__)

        v1, v2 = payload
        if v1 > v2:
            v1, v2 = v2, v1

        from sqlalchemy import or_
        value = attr_getter()
        return or_(value < v1, value > v2)

    # Custom, generic clauses.

    def _do_and(self, clause_name, payload):
        if not isinstance(payload, dict) or not len(payload):
            raise ServerError('can\'t parse "%s" clause: contents must be a dict, '
                              'but got %s', clause_name, payload.__class__.__name__)
        from sqlalchemy import and_
        return and_(*[self._compile_clause(*t) for t in payload.iteritems()])

    def _do_or(self, clause_name, payload):
        if not isinstance(payload, dict) or not len(payload):
            raise ServerError('can\'t parse "%s" clause: contents must be a dict, '
                              'but got %s', clause_name, payload.__class__.__name__)
        from sqlalchemy import or_
        return or_(*[self._compile_clause(*t) for t in payload.iteritems()])

    def _do_none_of(self, clause_name, payload):
        if not isinstance(payload, dict) or not len(payload):
            raise ServerError('can\'t parse "%s" clause: contents must be a dict, '
                              'but got %s', clause_name, payload.__class__.__name__)
        from sqlalchemy import not_, or_
        return not_(or_(*[self._compile_clause(*t) for t in payload.iteritems()]))

    def _do_always_true(self, clause_name, payload):
        """We just ignore the payload."""
        from sqlalchemy import literal
        return literal(True)

    def _do_always_false(self, clause_name, payload):
        """We just ignore the payload."""
        from sqlalchemy import literal
        return literal(False)


def _obs_get_num_files():
    from sqlalchemy import func
    from .file import File
    from .observation import Observation
    return db.session.query(func.count(File.name)).filter(File.obsid == Observation.obsid).as_scalar().alias('num_files')


simple_obs_attrs = [
    ('obsid', AttributeTypes.int, None),
    ('start_time_jd', AttributeTypes.float, None),
    ('stop_time_jd', AttributeTypes.float, None),
    ('start_lst_hr', AttributeTypes.float, None),
    ('session_id', AttributeTypes.int, None),
    ('duration', AttributeTypes.float, None),
    ('num_files', AttributeTypes.int, _obs_get_num_files),
]


class ObservationSearchCompiler(GenericSearchCompiler):
    def __init__(self):
        from .observation import Observation
        super(ObservationSearchCompiler, self).__init__()
        self._add_attributes(Observation, simple_obs_attrs)


the_obs_search_compiler = ObservationSearchCompiler()


def _file_get_num_instances():
    from sqlalchemy import func
    from .file import File, FileInstance
    return db.session.query(func.count()).filter(FileInstance.name == File.name).as_scalar().alias('num_instances')


simple_file_attrs = [
    ('name', AttributeTypes.string, None),
    ('type', AttributeTypes.string, None),
    ('source', AttributeTypes.string, None),
    ('size', AttributeTypes.int, None),
    ('obsid', AttributeTypes.int, None),
    ('num-instances', AttributeTypes.int, _file_get_num_instances),
]


class FileSearchCompiler(GenericSearchCompiler):
    def __init__(self):
        from .file import File
        super(FileSearchCompiler, self).__init__()
        self._add_attributes(File, simple_file_attrs)
        self.clauses['obs-matches'] = self._do_obs_matches

        self.clauses['name-like'] = self.clauses['name-matches']  # compat alias
        self.clauses['source-is'] = self.clauses['source-is-exactly']  # compat alias

        # These are technically properties of Observations, not Files, but
        # users aren't going to want to jump through extra hoops to query for
        # them, so we proxy the query clauses.

        from functools import partial
        for pfx in ('start-time-jd', 'stop-time-jd', 'start-lst-hr', 'session-id'):
            for cname in six.iterkeys(the_obs_search_compiler.clauses):
                if cname.startswith(pfx):
                    self.clauses[cname] = self._do_obs_sub_query

        # I named these in a very ... weird way.
        self.clauses['not-older-than'] = self._do_not_older_than
        self.clauses['not-newer-than'] = self._do_not_newer_than

    def _do_not_older_than(self, clause_name, payload):
        if not isinstance(payload, (int, float)):
            raise ServerError('can\'t parse "%s" clause: contents must be '
                              'numeric, but got %s', clause_name, payload.__class__.__name__)

        from .file import File
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=payload)
        return (File.create_time > cutoff)

    def _do_not_newer_than(self, clause_name, payload):
        if not isinstance(payload, (int, float)):
            raise ServerError('can\'t parse "%s" clause: contents must be '
                              'numeric, but got %s', clause_name, payload.__class__.__name__)

        from .file import File
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=payload)
        return (File.create_time < cutoff)

    def _do_obs_matches(self, clause_name, payload):
        from .observation import Observation
        from .file import File

        matched_obsids = (db.session.query(Observation.obsid)
                          .filter(the_obs_search_compiler.compile(payload)))
        return File.obsid.in_(matched_obsids)

    def _do_obs_sub_query(self, clause_name, payload):
        from .observation import Observation
        from .file import File

        matched_obsids = (db.session.query(Observation.obsid)
                          .filter(the_obs_search_compiler._compile_clause(clause_name, payload)))
        return File.obsid.in_(matched_obsids)


the_file_search_compiler = FileSearchCompiler()


def compile_search(search_string, query_type='files'):
    """This function returns a query on the File table that will return the File
    items matching the search.

    """
    from .file import File
    from .observation import Observation

    # As a convenience, we strip out #-delimited comments from the input text.
    # The default JSON parser doesn't accept them, but they're nice for users.

    def filter_comments():
        for line in search_string.splitlines():
            yield line.split('#', 1)[0]

    search_string = '\n'.join(filter_comments())

    # Parse JSON.

    try:
        search = json.loads(search_string)
    except Exception as e:
        raise ServerError('can\'t parse search as JSON: %s', e)

    # Offload to the helper classes.

    if query_type == 'files':
        return File.query.filter(the_file_search_compiler.compile(search))
    elif query_type == 'names':
        return db.session.query(File.name).filter(the_file_search_compiler.compile(search))
    elif query_type == 'obs':
        return Observation.query.filter(the_obs_search_compiler.compile(search))
    else:
        raise ServerError('unhandled query_type %r', query_type)


# "Standing orders" to copy files from one Librarian to another.

stord_logger = logging.getLogger('librarian.standingorders')


class StandingOrder(db.Model):
    """A StandingOrder describes a rule for copying data from this Librarian to
    another. We save a search and a destination. When new files match that
    search, we automatically start copying them to the destination. We create
    a FileEvent with a name based on the name of the StandingOrder to mark
    when a file has successfully been copied.

    It is assumed that the relevant search has some time limit applied so that
    only files created in the last (e.g.) 7 days match.

    """
    __tablename__ = 'standing_order'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = NotNull(db.String(64), unique=True)
    search = NotNull(db.Text)
    conn_name = NotNull(db.String(64))

    def __init__(self, name, search, conn_name):
        self.name = name
        self.search = search
        self.conn_name = conn_name
        self._validate()

    def _validate(self):
        """Check that this object's fields follow our invariants.

        """
        compile_search(self.search)  # will raise a ServerError if there's a problem.

    @property
    def event_type(self):
        return 'standing_order_succeeded:' + self.name

    def get_files_to_copy(self):
        """Generate a list of files that ought to be copied, according to the
        specifications of this StandingOrder.

        """
        from .file import File, FileEvent

        # The core query is something freeform specified by the user.

        query = compile_search(self.search)

        # We then layer on a check that the files don't have the specified
        # marker event.

        already_done = (db.session.query(File.name)
                        .filter(FileEvent.name == File.name,
                                FileEvent.type == self.event_type))
        query = query.filter(~File.name.in_(already_done))

        # Finally we filter out files that already have copy tasks associated
        # with this standing order, exceping those tasks that encountered an
        # error.

        from .store import UploaderTask
        from .bgtasks import the_task_manager

        already_launched = set(os.path.basename(t.store_path)
                               for t in the_task_manager.tasks
                               if (isinstance(t, UploaderTask) and
                                   self.name == t.standing_order_name and
                                   t.exception is None))

        for file in query:
            if file.name not in already_launched:
                yield file

    def maybe_launch_copies(self):
        """Launch any file copy operations that need to happen according to this
        StandingOrder's specification.

        """
        from .store import launch_copy_by_file_name
        stord_logger.debug('evaluating standing order %s', self.name)

        for file in self.get_files_to_copy():
            stord_logger.debug('got a hit: %s', file.name)
            if launch_copy_by_file_name(file.name, self.conn_name,
                                        standing_order_name=self.name, no_instance='return'):
                stord_logger.warn('standing order %s should copy file %s to %s, but no instances '
                                  'of it are available', self.name, file.name, self.conn_name)


# A simple little manager for running standing orders. We have a timeout to
# not evaluate them that often ... in the current setup, evaluating certain
# orders can be quite hard on the database.

MIN_STANDING_ORDER_INTERVAL = 1200  # seconds
DEFAULT_STANDING_ORDER_DELAY = 90  # seconds


def _launch_copy_timeout():
    stord_logger.debug('timeout invoked')

    if the_standing_order_manager.maybe_launch_copies():
        # The checks actually ran.
        the_standing_order_manager.launch_queued = False
    else:
        # We didn't run the checks because we did so recently. If a new file
        # was uploaded we want to make sure that it's eventually checked, so
        # re-queue ourselves to run again.
        from tornado.ioloop import IOLoop
        stord_logger.debug('re-scheduling timeout')
        IOLoop.instance().call_later(DEFAULT_STANDING_ORDER_DELAY, _launch_copy_timeout)


class StandingOrderManager(object):
    """A simple, singleton class for managing our standing orders.

    Other folks should primarily access the manager via the
    `queue_standing_order_copies` function. That function *queues* a command
    to examine our standing orders and launch any needed copy commands, with a
    default delay of 90 seconds. The delay is in place since uploads of files
    to the Librarian are likely to occur in batches, but it's easiest to just
    command the manager to "do its thing" whenever a file is uploaded. The
    delay makes it so that when we actually look for files to copy, there's
    probably a bunch of them ready to go, not just the very first one that was
    uploaded.

    """
    last_check = 0
    launch_queued = False

    def maybe_launch_copies(self):
        """Returns True unless nothing happened because we've run a search recently.

        """
        now = time.time()

        if now - self.last_check < MIN_STANDING_ORDER_INTERVAL:
            return False  # Don't evaluate too often

        # Check if there are any restrictions on what we do with standing
        # orders. TODO: it's been requested that we also add time constraints
        # on the uploads (Github issue #23).

        mode = app.config.get('standing_order_mode', 'normal')

        if mode == 'disabled':
            stord_logger.debug('not checking standing orders: explicitly disabled')
            return True
        elif mode == 'nighttime':
            hour = time.localtime(now).tm_hour
            if hour >= 8 and hour < 20:
                stord_logger.debug('not checking standing orders: "nighttime" '
                                   'mode and hour = %d', hour)
                return True
        elif mode != 'normal':
            stord_logger.warn('unrecognized standing_order_mode %r; treating as "normal"', mode)
            mode = 'normal'

        stord_logger.debug('running searches')
        self.last_check = now

        for storder in StandingOrder.query.all():
            storder.maybe_launch_copies()

        return True

    def queue_launch_copy(self):
        """Queue a main-thread callback to check whether we need to launch any copies
        associated with our standing orders.

        """
        stord_logger.debug('called queue_launch_copy')
        if self.launch_queued:
            return

        self.launch_queued = True
        from tornado.ioloop import IOLoop
        stord_logger.debug('timeout actually scheduled')
        IOLoop.instance().call_later(DEFAULT_STANDING_ORDER_DELAY, _launch_copy_timeout)


the_standing_order_manager = StandingOrderManager()


def queue_standing_order_copies():
    stord_logger.debug('queueing check of standing orders')
    the_standing_order_manager.queue_launch_copy()


def register_standing_order_checkin():
    """Create a Tornado PeriodicCallback that will periodically check the
    standing orders to see if there's anything to do.

    Since we know all events related to files, in theory this shouldn't be
    needed, but in practice this can't hurt.

    The timeout for the callback is measured in milliseconds, so we queue an
    evaluation every 10 minutes.

    """
    from tornado import ioloop

    cb = ioloop.PeriodicCallback(queue_standing_order_copies, 60 * 10 * 1000)
    cb.start()
    return cb


# Web user interface

@app.route('/standing-orders')
@login_required
def standing_orders():
    q = StandingOrder.query.order_by(StandingOrder.name.asc())

    return render_template(
        'standing-order-listing.html',
        title='Standing Orders',
        storders=q,
    )


@app.route('/standing-orders/<string:name>')
@login_required
def specific_standing_order(name):
    storder = StandingOrder.query.filter(StandingOrder.name == name).first()
    if storder is None:
        flash('No such standing order "%s"' % name)
        return redirect(url_for('standing_orders'))

    try:
        cur_files = list(storder.get_files_to_copy())
    except Exception as e:
        flash('Cannot run this order’s search: %s' % e)
        cur_files = []

    return render_template(
        'standing-order-individual.html',
        title='Standing Order %s' % (storder.name),
        storder=storder,
        cur_files=cur_files,
    )


default_search = """{
  "name-matches": "any-file-named-like-%-this",
  "not-older-than": 14 # days
}"""


@app.route('/standing-orders/<string:ignored_name>/create', methods=['POST'])
@login_required
def create_standing_order(ignored_name):
    """Note that we ignore the order name and instead takes its value from the
    POST data; this is basically an implementation/consistency thing.

    """
    name = required_arg(request.form, unicode, 'name')

    try:
        if not len(name):
            raise Exception('order name may not be empty')

        storder = StandingOrder(name, default_search, 'undefined-connection')
        storder._validate()
        db.session.add(storder)
        db.session.commit()
    except Exception as e:
        flash('Cannot create "%s": %s' % (name, e))
        return redirect(url_for('standing_orders'))

    return redirect(url_for('standing_orders') + '/' + name)


@app.route('/standing-orders/<string:name>/update', methods=['POST'])
@login_required
def update_standing_order(name):
    storder = StandingOrder.query.filter(StandingOrder.name == name).first()
    if storder is None:
        flash('No such standing order "%s"' % name)
        return redirect(url_for('standing_orders'))

    new_name = required_arg(request.form, unicode, 'name')
    new_conn = required_arg(request.form, unicode, 'conn')
    new_search = required_arg(request.form, unicode, 'search')

    try:
        storder.name = new_name
        storder.conn_name = new_conn
        storder.search = new_search
        storder._validate()
        db.session.merge(storder)
        db.session.commit()
    except Exception as e:
        flash('Cannot update "%s": %s' % (name, e))
        return redirect(url_for('standing_orders'))

    # There might be new things to look at!
    queue_standing_order_copies()

    flash('Updated standing order "%s"' % new_name)
    return redirect(url_for('standing_orders'))


@app.route('/standing-orders/<string:name>/delete', methods=['POST'])
@login_required
def delete_standing_order(name):
    storder = StandingOrder.query.filter(StandingOrder.name == name).first()
    if storder is None:
        flash('No such standing order "%s"' % name)
        return redirect(url_for('standing_orders'))

    db.session.delete(storder)
    db.session.commit()

    flash('Deleted standing order "%s"' % name)
    return redirect(url_for('standing_orders'))


# Web interface to searches outside of the standing order system

sample_file_search = '{ "name-matches": "%12345%.uv" }'


@app.route('/search-files', methods=['GET', 'POST'])
@login_required
def search_files():
    return render_template(
        'search-files.html',
        title='Search Files',
        sample_search=sample_file_search,
    )


sample_obs_search = '{ "duration-less-than": 0.003 }'


@app.route('/search-obs', methods=['GET', 'POST'])
@login_required
def search_obs():
    return render_template(
        'search-obs.html',
        title='Search Observations',
        sample_search=sample_obs_search,
    )


# These formats are defined in templates/search-*.html:
file_name_format = 'Raw text with file names'
full_path_format = 'Raw text with full instance paths'
human_file_format = 'List of files'
human_obs_format = 'List of observations'


@app.route('/search', methods=['GET', 'POST'])
@login_required
def execute_search():
    if len(request.form):
        reqdata = request.form
    else:
        reqdata = request.args

    query_type = required_arg(reqdata, unicode, 'type')
    search_text = required_arg(reqdata, unicode, 'search')
    output_format = optional_arg(reqdata, unicode, 'output_format', 'ui')
    for_humans = True

    if output_format == full_path_format:
        for_humans = False
        query_type = 'names'
    elif output_format == file_name_format:
        for_humans = False
    elif output_format == human_file_format:
        for_humans = True
    elif output_format == human_obs_format:
        for_humans = True
    else:
        return Response('Illegal search output type %r' % (output_format, ), status=400)

    status = 200

    if for_humans:
        mimetype = 'text/html'
    else:
        mimetype = 'text/plain'

    try:
        search = compile_search(search_text, query_type=query_type)

        if output_format == full_path_format:
            from .file import FileInstance
            instances = FileInstance.query.filter(FileInstance.name.in_(search))
            text = '\n'.join(i.full_path_on_store() for i in instances)
        elif output_format == file_name_format:
            text = '\n'.join(f.name for f in search)
        elif output_format == human_file_format:
            files = list(search)
            text = render_template(
                'search-results-file.html',
                title='Search Results: %d Files' % len(files),
                search_text=search_text,
                files=files,
                error_message=None,
            )
        elif output_format == human_obs_format:
            obs = list(search)
            text = render_template(
                'search-results-obs.html',
                title='Search Results: %d Observations' % len(obs),
                search_text=search_text,
                obs=obs,
                error_message=None,
            )
        else:
            raise ServerError('internal logic failure mishandled output format')
    except Exception as e:
        import sys
        app.log_exception(sys.exc_info())
        status = 400

        if for_humans:
            text = render_template(
                'search-results-file.html',
                title='Search Results: Error',
                search_text=search_text,
                files=[],
                error_message=str(e),
            )
        else:
            text = 'Search resulted in error: %s' % e

    return Response(text, status=status, mimetype=mimetype)
