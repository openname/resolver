# -*- coding: utf-8 -*-
"""
    Resolver
    ~~~~~
    :copyright: (c) 2014-2016 by Halfmoon Labs, Inc.
    :copyright: (c) 2016 blockstack.org
    :license: MIT, see LICENSE for more details.
"""

import re
import json
import collections
import pylibmc
import logging

from flask import Flask, make_response, jsonify, abort, request
from time import time
from basicrpc import Proxy

from blockstack_proofs import profile_to_proofs, profile_v3_to_proofs
from blockstack_profiles import resolve_zone_file_to_profile
from blockstack_profiles import is_profile_in_legacy_format

from .crossdomain import crossdomain

from .config import DEBUG
from .config import DEFAULT_HOST, MEMCACHED_SERVERS, MEMCACHED_USERNAME
from .config import MEMCACHED_PASSWORD, MEMCACHED_TIMEOUT, MEMCACHED_ENABLED
from .config import USERSTATS_TIMEOUT
from .config import VALID_BLOCKS, RECENT_BLOCKS
from .config import BLOCKSTACKD_IP, BLOCKSTACKD_PORT
from .config import DHT_MIRROR_IP, DHT_MIRROR_PORT
from .config import DEFAULT_NAMESPACE
from .config import NAMES_FILE

import requests
requests.packages.urllib3.disable_warnings()

app = Flask(__name__)

logging.basicConfig()
log = logging.getLogger('resolver')

if DEBUG:
    log.setLevel(level=logging.DEBUG)
else:
    log.setLevel(level=logging.INFO)


def get_mc_client():
    """ Return a new connection to memcached
    """

    mc = pylibmc.Client(MEMCACHED_SERVERS, binary=True,
                        username=MEMCACHED_USERNAME,
                        password=MEMCACHED_PASSWORD,
                        behaviors={"no_block": True,
                                   "connect_timeout": 200})

    return mc

mc = get_mc_client()


def validName(name):
    """ Return True if valid name
    """

    # current regrex doesn't account for .namespace
    regrex = re.compile('^[a-z0-9_]{1,60}$')

    if regrex.match(name):
        return True
    else:
        return False


def fetch_from_dht(profile_hash):
    """ Given a @profile_hash fetch full profile JSON
    """

    dht_client = Proxy(DHT_MIRROR_IP, DHT_MIRROR_PORT)

    try:
        dht_resp = dht_client.get(profile_hash)
    except:
        #abort(500, "Connection to DHT timed out")
        return {"error": "Data not saved in DHT yet."}

    dht_resp = dht_resp[0]

    if dht_resp is None:
        return {"error": "Data not saved in DHT yet."}

    return dht_resp['value']


def fetch_proofs(profile, username, profile_ver=2, refresh=False):
    """ Get proofs for a profile and:
        a) check cached entries
        b) check which version of profile we're using
    """

    if MEMCACHED_ENABLED and not refresh:
        log.debug("Memcache get proofs: %s" % username)
        proofs_cache_reply = mc.get("proofs_" + str(username))
    else:
        proofs_cache_reply = None

    if proofs_cache_reply is None:

        if profile_ver == 3:
            proofs = profile_v3_to_proofs(profile, username)
        else:
            proofs = profile_to_proofs(profile, username)

        if MEMCACHED_ENABLED or refresh:
            log.debug("Memcache set proofs: %s" % username)
            mc.set("proofs_" + str(username), json.dumps(proofs),
                   int(time() + MEMCACHED_TIMEOUT))
    else:

        proofs = json.loads(proofs_cache_reply)

    return proofs


def format_profile(profile, username, address, refresh=False):
    """ Process profile data and
        1) Insert verifications
        2) Check if profile data is valid JSON
    """

    data = {}

    # save the original profile, in case it's a zone file
    zone_file = profile

    if 'error' in profile:
        data['profile'] = {}
        data['error'] = profile['error']
        data['verifications'] = []

        return data

    try:
        profile = resolve_zone_file_to_profile(profile, address)
    except:
        if 'message' in profile:
            data['profile'] = json.loads(profile)
            data['verifications'] = []
            return data

    if profile is None:
        data['profile'] = {}
        data['error'] = "Malformed profile data."
        data['verifications'] = []
    else:
        if not is_profile_in_legacy_format(profile):
            data['zone_file'] = zone_file
            data['profile'] = profile
            data['verifications'] = fetch_proofs(data['profile'], username,
                                                 profile_ver=3, refresh=refresh)
        else:
            data['profile'] = json.loads(profile)
            data['verifications'] = fetch_proofs(data['profile'], username,
                                                 refresh=refresh)

    return data


def get_profile(username, refresh=False, namespace=DEFAULT_NAMESPACE):
    """ Given a fully-qualified username (username.namespace)
        get the data associated with that fqu.
        Return cached entries, if possible.
    """

    global MEMCACHED_ENABLED
    global mc

    username = username.lower()

    if MEMCACHED_ENABLED and not refresh:
        log.debug("Memcache get DHT: %s" % username)
        dht_cache_reply = mc.get("dht_" + str(username))
    else:
        log.debug("Memcache disabled: %s" % username)
        dht_cache_reply = None

    if dht_cache_reply is None:

        try:
            bs_client = Proxy(BLOCKSTACKD_IP, BLOCKSTACKD_PORT)
            bs_resp = bs_client.get_name_blockchain_record(username + "." + namespace)
            bs_resp = bs_resp[0]
        except:
            abort(500, "Connection to blockstack-server %s:%s timed out" % (BLOCKSTACKD_IP, BLOCKSTACKD_PORT))

        if bs_resp is None:
            abort(404)

        if 'value_hash' in bs_resp:
            profile_hash = bs_resp['value_hash']
            dht_response = fetch_from_dht(profile_hash)

            dht_data = {}
            dht_data['dht_response'] = dht_response
            dht_data['owner_address'] = bs_resp['address']

            if MEMCACHED_ENABLED or refresh:
                log.debug("Memcache set DHT: %s" % username)
                mc.set("dht_" + str(username), json.dumps(dht_data),
                       int(time() + MEMCACHED_TIMEOUT))
        else:
            dht_data = {"error": "Not found"}
    else:
        dht_data = json.loads(dht_cache_reply)

    data = format_profile(dht_data['dht_response'], username, dht_data['owner_address'])

    return data


def get_all_users():
    """ Return all users in the .id namespace
    """

    try:
        fout = open(NAMES_FILE, 'r')
        data = fout.read()
        data = json.loads(data)
        fout.close()
    except:
        data = {}

    return data


@app.route('/v2/users/<usernames>', methods=['GET'], strict_slashes=False)
@crossdomain(origin='*')
def get_users(usernames):
    """ Fetch data from username in .id namespace
    """

    reply = {}
    refresh = False

    try:
        refresh = request.args.get('refresh')
    except:
        pass

    if usernames is None:
        reply['error'] = "No usernames given"
        return jsonify(reply)

    if ',' not in usernames:

        username = usernames

        info = get_profile(username, refresh=refresh)

        if 'error' in info:
            reply[username] = info
            return jsonify(reply), 502
        else:
            reply[username] = info

        return jsonify(reply), 200

    try:
        usernames = usernames.rsplit(',')
    except:
        reply['error'] = "Invalid input format"
        return jsonify(reply)

    for username in usernames:

        try:
            profile = get_profile(username, refresh=refresh)

            if 'error' in profile:
                pass
            else:
                reply[username] = profile
        except:
            pass

    return jsonify(reply), 200


@app.route('/v2/namespace', strict_slashes=False)
@crossdomain(origin='*')
def get_namespace():
    """ Get stats on registration and all names registered
        (old endpoint, still here for compatibility)
    """

    reply = {}
    total_users = get_all_users()
    reply['stats'] = {'registrations': len(total_users)}
    reply['usernames'] = total_users

    return jsonify(reply)


@app.route('/v2/namespaces', strict_slashes=False)
@crossdomain(origin='*')
def get_all_namespaces():
    """ Get stats on registration and all names registered
    """

    json.encoder.c_make_encoder = None

    reply = {}
    all_namespaces = []
    total_users = get_all_users()

    id_namespace = collections.OrderedDict([("namespace", "id"),
                                            ("registrations", len(total_users)),
                                            ("names", total_users)])

    all_namespaces.append(id_namespace)

    reply['namespaces'] = all_namespaces

    # disable Flask's JSON sorting
    app.config["JSON_SORT_KEYS"] = False

    return jsonify(reply)


@app.route('/v2/users/', methods=['GET'], strict_slashes=False)
@crossdomain(origin='*')
def get_user_count():
    """ Get stats on registered names
    """

    reply = {}

    total_users = get_all_users()
    reply['stats'] = {'registrations': len(total_users)}

    return jsonify(reply)


@app.route('/')
def index():
    """ Default HTML display if someone visits the resolver
    """

    reply = '<hmtl><body>Welcome to this Blockstack resolver, see \
            <a href="http://github.com/blockstack/blockstack-resolver"> \
            this Github repo</a> for details.</body></html>'

    return reply


@app.errorhandler(500)
def internal_error(error):
    return make_response(jsonify({'error': error.description}), 500)


@app.errorhandler(404)
def not_found(error):
    return make_response(jsonify({'error': 'Not found'}), 404)
