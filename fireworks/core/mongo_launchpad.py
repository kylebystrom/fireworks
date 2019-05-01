# coding: utf-8

from __future__ import unicode_literals

from monty.io import zopen
from monty.os.path import zpath

"""
The LaunchPad manages the Fireworks database.
"""

import datetime
import json
import os
import random
import time
import traceback
import shutil
import gridfs
from collections import OrderedDict, defaultdict
from itertools import chain
from tqdm import tqdm
from bson import ObjectId

from pymongo import MongoClient
from pymongo import DESCENDING, ASCENDING
from pymongo.errors import DocumentTooLarge
from monty.serialization import loadfn
from pymongo.collection import Collection

from fireworks.core.firework import Firework, Workflow, Firetask
from fireworks.core.launchpad import LaunchPad, LockedWorkflowError, WFLock
from fireworks.fw_config import LAUNCHPAD_LOC, SORT_FWS, RESERVATION_EXPIRATION_SECS, \
    RUN_EXPIRATION_SECS, MAINTAIN_INTERVAL, WFLOCK_EXPIRATION_SECS, WFLOCK_EXPIRATION_KILL, \
    MONGO_SOCKET_TIMEOUT_MS, GRIDFS_FALLBACK_COLLECTION, FWORKER_LOC
from fireworks.utilities.fw_serializers import FWSerializable, reconstitute_dates
from fireworks.core.firework import Firework, Workflow, FWAction, Tracker
from fireworks.utilities.fw_utilities import get_fw_logger
from fireworks.utilities.fw_serializers import recursive_dict, _recursive_load

from typing import TypeVar, List, Tuple, Dict, Union, Optional, Any

Num = TypeVar('Num', int, float)

__author__ = 'Anubhav Jain'
__copyright__ = 'Copyright 2013, The Materials Project'
__version__ = '0.1'
__maintainer__ = 'Anubhav Jain'
__email__ = 'ajain@lbl.gov'
__date__ = 'Jan 30, 2013'

# TODO: lots of duplication reduction and cleanup possible

class MongoLaunchPad(LaunchPad):
    """
    The LaunchPad manages the Fireworks database.
    """

    def __init__(self, host: str=None, port: int=None, name: str=None,
                 username: str=None, password: str=None,
                 logdir: str=None, strm_lvl: str=None, user_indices: list=None,
                 wf_user_indices: list=None, ssl: bool=False,
                 ssl_ca_certs: str=None, ssl_certfile: str=None,
                 ssl_keyfile: str=None, ssl_pem_passphrase: str=None,
                 authsource: str=None,
                 fworker: Optional[Union[str, Dict]]=None):
        """
        Args:
            host (str): hostname. A MongoDB connection string URI (https://docs.mongodb.com/manual/reference/connection-string/) can be used instead of the remaining options below.
            port (int): port number
            name (str): database name
            username (str)
            password (str)
            logdir (str): path to the log directory
            strm_lvl (str): the logger stream level
            user_indices (list): list of 'fireworks' collection indexes to be built
            wf_user_indices (list): list of 'workflows' collection indexes to be built
            ssl (bool): use TLS/SSL for mongodb connection
            ssl_ca_certs (str): path to the CA certificate to be used for mongodb connection
            ssl_certfile (str): path to the client certificate to be used for mongodb connection
            ssl_keyfile (str): path to the client private key
            ssl_pem_passphrase (str): passphrase for the client private key
            authsource (str): authsource parameter for MongoDB authentication; defaults to "name" (i.e., db name) if not set
        """

        # detect if connection_string mode
        host_uri_mode = False
        if host is not None and (host.startswith("mongodb://") or
                                 host.startswith("mongodb+srv://")):
            host_uri_mode = True

        self.host = host if (host or host_uri_mode) else "localhost"
        self.port = port if (port or host_uri_mode) else 27017
        self.name = name if (name or host_uri_mode) else "fireworks"
        self.username = username
        self.password = password
        self.ssl = ssl
        self.ssl_ca_certs = ssl_ca_certs
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_pem_passphrase = ssl_pem_passphrase
        self.authsource = authsource or self.name

        # set up logger
        self.logdir = logdir
        self.strm_lvl = strm_lvl if strm_lvl else 'INFO'
        self.m_logger = get_fw_logger('launchpad', l_dir=self.logdir, stream_level=self.strm_lvl)

        self.user_indices = user_indices if user_indices else []
        self.wf_user_indices = wf_user_indices if wf_user_indices else []

        # get connection
        if host_uri_mode:
            self.connection = MongoClient(host)
            try:
                option_idx = host.index("?")
                host = host[:option_idx]
            except ValueError:
                pass
            self.db = self.connection[host.split("/")[-1]]
        else:
            self.connection = MongoClient(self.host, self.port, ssl=self.ssl,
                                          ssl_ca_certs=self.ssl_ca_certs,
                                          ssl_certfile=self.ssl_certfile,
                                          ssl_keyfile=self.ssl_keyfile,
                                          ssl_pem_passphrase=self.ssl_pem_passphrase,
                                          socketTimeoutMS=MONGO_SOCKET_TIMEOUT_MS,
                                          username=self.username,
                                          password=self.password,
                                          authSource=self.authsource)
            self.db = self.connection[self.name]

        self.fireworks = self.db.fireworks
        self.launches = self.db.launches
        self.offline_runs = self.db.offline_runs
        self.fw_id_assigner = self.db.fw_id_assigner
        self.workflows = self.db.workflows
        if GRIDFS_FALLBACK_COLLECTION:
            self.gridfs_fallback = gridfs.GridFS(self.db, GRIDFS_FALLBACK_COLLECTION)
        else:
            self.gridfs_fallback = None

        self.backup_launch_data = {}
        self.backup_fw_data = {}

        if fworker is None and FWORKER_LOC:
            fworker = loadfn(FWORKER_LOC)
        elif type(fworker) == str:
            fworker = loadfn(fworker)
        self.fworker = fworker or {'name': 'my first fireworker',
                                   'category': '',
                                   'query': {}}

    @property
    def worker_query(self) -> Dict:
        """
        Returns updated query dict.
        """
        q = dict(self.fworker.get('query'))
        fworker_check = [{"spec._fworker": {"$exists": False}},
                         {"spec._fworker": None},
                         {"spec._fworker": self.fworker.get('name')}]
        category = self.fworker.get('category')
        if '$or' in q:
            q['$and'] = q.get('$and', [])
            q['$and'].extend([{'$or': q.pop('$or')}, {'$or': fworker_check}])
        else:
            q['$or'] = fworker_check
        if category and isinstance(self.fworker.get('category'), six.string_types):
            if category == "__none__":
                q['spec._category'] = {"$exists": False}
            else:
                q['spec._category'] = self.fworker.get('category')
        elif category:  # category is list of str
            q['spec._category'] = {"$in": self.fworker.get('category')}

        return q

    def to_dict(self) -> Dict:
        """
        Note: usernames/passwords are exported as unencrypted Strings!
        """
        return {
            'host': self.host,
            'port': self.port,
            'name': self.name,
            'username': self.username,
            'password': self.password,
            'logdir': self.logdir,
            'strm_lvl': self.strm_lvl,
            'user_indices': self.user_indices,
            'wf_user_indices': self.wf_user_indices,
            'ssl': self.ssl,
            'ssl_ca_certs': self.ssl_ca_certs,
            'ssl_certfile': self.ssl_certfile,
            'ssl_keyfile': self.ssl_keyfile,
            'ssl_pem_passphrase': self.ssl_pem_passphrase,
            'authsource': self.authsource,
            'fworker': self.fworker}

    def update_spec(self, fw_ids: List[int], spec_document: Dict):
        """
        Update fireworks with a spec. Sometimes you need to modify a firework in progress.

        Args:
            fw_ids [int]: All fw_ids to modify.
            spec_document (dict): The spec document. Note that only modifications to
                the spec key are allowed. So if you supply {"_tasks.1.parameter": "hello"},
                you are effectively modifying spec._tasks.1.parameter in the actual fireworks
                collection.
            mongo (bool): spec_document uses mongo syntax to directly update the spec
        """
        mod_spec = {"$set": {("spec." + k): v for k, v in spec_document.items()} }

        allowed_states = ["READY", "WAITING", "FIZZLED", "DEFUSED", "PAUSED"]
        self.fireworks.update_many({'fw_id': {"$in": fw_ids},
                                    'state': {"$in": allowed_states}}, mod_spec)

        for fw in self.fireworks.find({'fw_id': {"$in": fw_ids}, 'state': {"$nin": allowed_states}},
                                      {"fw_id": 1, "state": 1}):
            self.m_logger.warning("Cannot update spec of fw_id: {} with state: {}. "
                               "Try rerunning first".format(fw['fw_id'], fw['state']))

    @classmethod
    def from_dict(cls, d: Dict) -> 'MongoLaunchPad':
        port = d.get('port', None)
        name = d.get('name', None)
        username = d.get('username', None)
        password = d.get('password', None)
        logdir = d.get('logdir', None)
        strm_lvl = d.get('strm_lvl', None)
        user_indices = d.get('user_indices', [])
        wf_user_indices = d.get('wf_user_indices', [])
        ssl = d.get('ssl', False)
        ssl_ca_certs = d.get('ssl_ca_certs', d.get('ssl_ca_file', None))  # ssl_ca_file was the old notation for FWS < 1.5.5
        ssl_certfile = d.get('ssl_certfile', None)
        ssl_keyfile = d.get('ssl_keyfile', None)
        ssl_pem_passphrase = d.get('ssl_pem_passphrase', None)
        authsource= d.get('authsource', None)
        return MongoLaunchPad(d['host'], port, name, username, password,
                         logdir, strm_lvl, user_indices, wf_user_indices, ssl,
                         ssl_ca_certs, ssl_certfile, ssl_keyfile, ssl_pem_passphrase,
                         authsource)

    @property
    def workflow_count(self) -> int:
        return self.workflows.count_documents({})

    @property
    def firework_count(self) -> int:
        return self.fireworks.count_documents({})

    def _reset(self):
        self.fireworks.delete_many({})
        self.launches.delete_many({})
        self.workflows.delete_many({})
        #self.offline_runs.delete_many({})
        self._restart_ids(1, 1)
        if self.gridfs_fallback is not None:
            self.db.drop_collection("{}.chunks".format(GRIDFS_FALLBACK_COLLECTION))
            self.db.drop_collection("{}.files".format(GRIDFS_FALLBACK_COLLECTION))
        self.tuneup()
        self.m_logger.info('LaunchPad was RESET.')

    def _get_duplicates(self, fw_id: int, include_self: bool=False,
                        allowed_states: List[str]=None) -> List[int]:
        if type(allowed_states) == str:
            allowed_states = {'$in': allowed_states}
        f = self.fireworks.find_one({"fw_id": fw_id, "spec._dupefinder": {"$exists": True}},
                                    {'launches':1})
        duplicates = []
        if f:
            query = {"launches": {"$in": f['launches']}}
            if allowed_states:
                query['state'] = allowed_states
            if not include_self:
                query['fw_id'] = {"$ne": fw_id}
            for d in self.fireworks.find(query, {"fw_id": 1}):
                duplicates.append(d['fw_id'])
        return list(set(duplicates))

    def _recover(self, fw_id: int, launch_idx: int=None, recover_mode: str='prev_dir'):
        """
        Function to get recovery data for a given fw
        Args:
            fw_id (int): fw id to get recovery data for
            launch_id (int or 'last'): launch_id to get recovery data for, if 'last'
                recovery data is generated from last launch
        """
        if launch_idx is not None:
            m_fw = self.get_fw_by_id(fw_id, launch_idx)
            recovery = m_fw.state_history[-1].get("checkpoint", {})
            #if recovery:
            recovery.update({'_prev_dir': m_fw.launch_dir,
                             '_launch_idx': m_fw.launch_idx})
            # Launch recovery
            recovery.update({'_mode': recover_mode})
            set_spec = {'$set': {'spec._recovery': recovery}}
            if recover_mode == 'prev_dir':
                prev_dir = m_fw.launch_dir
                set_spec['$set']['spec._launch_dir'] = prev_dir

        # If no launch recovery specified, unset the firework recovery spec
        else:
            set_spec = {"$unset":{"spec._recovery":""}}
        
        self.fireworks.find_one_and_update({"fw_id":fw_id}, set_spec)

    def _get_launch_by_fw_id(self, fw_id: int, launch_idx: int=None) -> Dict:
        """
        Given a Firework id, return launches.

        Args:
            launch_id (int): launch id

        Returns:
            dict
        """
        if launch_idx == None:
            #m_launches = self.launches.find({'fw_id': fw_id})
            m_launches = []
            launch_ids = self.fireworks.find_one({'fw_id': fw_id})['launches']
            if launch_ids:
                for i, lid in enumerate(launch_ids):
                    m_launch = self.launches.find_one({'launch_id': lid})
                    m_launch["action"] = get_action_from_gridfs(m_launch.get("action"),
                                                                self.gridfs_fallback)
                    #if 'launch_id' in m_launch:
                    #    m_launch.pop('launch_id')
                    m_launch['launch_idx'] = i
                    m_launches.append(m_launch)
            return m_launches
        else:
            launch_ids = self.fireworks.find_one({'fw_id': fw_id}, projection={'launches': 1})['launches']
            if len(launch_ids) == 0:
                return None
            if launch_idx >= len(launch_ids) or launch_idx < -len(launch_ids):
                raise ValueError("Bad launch index %d %d" % (launch_idx, len(launch_ids)))
            launch_id = launch_ids[launch_idx]
            launch = self.launches.find_one({'launch_id': launch_id})
            launch['launch_idx'] = launch_idx if launch_idx >= 0\
                                   else len(launch_ids)+launch_idx
            if 'launch_id' in launch:
                launch.pop('launch_id')
            launch["action"] = get_action_from_gridfs(launch.get("action"),
                                                      self.gridfs_fallback)
            return launch
        raise ValueError('No Launch exists with launch_idx: {}'.format(launch_idx))

    def get_fw_dict_by_id(self, fw_id: int, launch_idx: int=-1) -> Dict:
        """
        Given firework id, return firework dict.

        Args:
            fw_id (int): firework id

        Returns:
            dict
        """
        fw_dict = self.fireworks.find_one({'fw_id': fw_id})

        if not fw_dict:
            raise ValueError('No Firework exists with id: {}'.format(fw_id))

        launch = None
        if launch_idx and fw_dict["state"] not in ["WAITING", "READY"]:
            launch = self._get_launch_by_fw_id(fw_id, launch_idx)
        if launch is None:
            launch = {"state": fw_dict["state"]}

        fw_dict['launch'] = launch
        fw_dict.pop('launches')
        return fw_dict

    def _delete_wf(self, fw_id: int, fw_ids: List[int]) -> List[str]:
        # TODO COME BACK TO THIS
        potential_launches = []
        launch_ids = []
        launch_dirs = []
        for i in fw_ids:
            launches = self._get_launch_by_fw_id(i)
            print('found launches for fw %d' % i, launches)
            potential_launches += [l['launch_id'] for l in launches]
        print(potential_launches)

        for i in potential_launches:  # only remove launches if no other fws refer to them
            if not self.fireworks.find_one({'$or': [{"launches": i}, {'archived_launches': i}],
                                            'fw_id': {"$nin": fw_ids}}, {'launch_id': 1}):
                launch_ids.append(i)
                launch_dirs.append(self.launches.find_one({'launch_id': i}, {'launch_dir': 1})['launch_dir'])
            print(launch_dirs)

        if self.gridfs_fallback is not None:
            for lid in launch_ids:
                for f in self.gridfs_fallback.find({"metadata.launch_id": lid}):
                    self.gridfs_fallback.delete(f._id)
        self.launches.delete_many({'launch_id': {"$in": launch_ids}})
        self.offline_runs.delete_many({'launch_id': {"$in": launch_ids}})
        self.fireworks.delete_many({"fw_id": {"$in": fw_ids}})
        self.workflows.delete_one({'nodes': fw_id})

        return launch_dirs

    def _insert_wfs(self, wfs: Union[Workflow, List[Workflow]]):
        if type(wfs) == Workflow:
            self.workflows.insert_one(wfs.to_db_dict())
        else:
            self.workflows.insert_many(wf.to_db_dict() for wf in wfs)

    def _insert_fws(self, fws: Union[Firework, List[Firework]]):
        if type(fws) == Firework:
            self.fireworks.insert_one(fws.to_db_dict())
        else:
            self.fireworks.insert_many(fw.to_db_dict() for fw in fws)

    def _delete_fws(self, fw_ids: List[int]):
        self.fireworks.delete_many({'fw_id': {'$in': fw_ids}})

    def _get_wf_data(self, fw_id: int, mode: str='more') -> Dict:
        # THIS OVERRIDES A DEFAULT _get_wf_data
        wf_fields = ["state", "created_on", "name", "nodes"]
        fw_fields = ["state", "fw_id"]
        launch_fields = []

        if mode != "less":
            wf_fields.append("updated_on")
            fw_fields.extend(["name", "launches"])
            launch_fields.append("launch_id")
            launch_fields.append("launch_dir")

        if mode == "reservations":
            launch_fields.append("state_history.reservation_id")

        if mode == "all":
            wf_fields = None

        wf = self.workflows.find_one({"nodes": fw_id}, projection=wf_fields)
        fw_data = []
        id_name_map = {}
        launch_ids = []
        for fw in self.fireworks.find({"fw_id": {"$in": wf["nodes"]}}, projection=fw_fields):
            if launch_fields:
                launch_ids.extend(fw["launches"])
            fw_data.append(fw)
            if mode != "less":
                id_name_map[fw["fw_id"]] = "%s--%d" % (fw["name"], fw["fw_id"])

        if launch_fields:
            launch_info = defaultdict(list)
            for l in self.launches.find({'launch_id': {"$in": launch_ids}}, projection=launch_fields):
                for i, fw in enumerate(fw_data):
                    if l["launch_id"] in fw["launches"]:
                        launch_info[i].append(l)
            for k, v in launch_info.items():
                fw_data[k]["launches"] = v

        wf["fw"] = fw_data
        return wf

    def get_fw_ids(self, query: Dict=None, sort: List[Tuple]=None,
                   limit: int=0, count_only: bool=False,
                   launches_mode: bool=False) -> List[int]:
        """
        Return all the fw ids that match a query.

        Args:
            query (dict): representing a Mongo query
            sort [(str,str)]: sort argument in Pymongo format
            limit (int): limit the results
            count_only (bool): only return the count rather than explicit ids
            launches_mode (bool): query the launches collection instead of fireworks

        Returns:
            list: list of firework ids matching the query
        """
        fw_ids = []
        coll = "fireworks"
        criteria = query if query else {}

        if count_only:
            if limit:
                return ValueError("Cannot count_only and limit at the same time!")
            return getattr(self, coll).count_documents(criteria)

        for fw in getattr(self, coll).find(criteria, {"fw_id": True}, sort=sort).limit(limit):
            fw_ids.append(fw["fw_id"])
        return fw_ids

    def get_wf_ids(self, query: Dict=None, sort: List[Tuple]=None,
                   limit: int=0, count_only: bool=False) -> List[int]:
        """
        Return one fw id for all workflows that match a query.

        Args:
            query (dict): representing a Mongo query
            sort [(str,str)]: sort argument in Pymongo format
            limit (int): limit the results
            count_only (bool): only return the count rather than explicit ids

        Returns:
            list: list of firework ids
        """
        wf_ids = []
        criteria = query if query else {}
        if count_only:
            return self.workflows.find(criteria, {"nodes": True},
                sort=sort).limit(limit).count_documents()

        for fw in self.workflows.find(criteria, {"nodes": True}, sort=sort).limit(limit):
            wf_ids.append(fw["nodes"][0])

        return wf_ids

    def tuneup(self, bkground: bool=True):
        """
        Database tuneup: build indexes
        """
        self.m_logger.info('Performing db tune-up')

        self.m_logger.debug('Updating indices...')
        self.fireworks.create_index('fw_id', unique=True, background=bkground)
        for f in ("state", 'spec._category', 'created_on', 'updated_on' 'name', 'launches'):
            self.fireworks.create_index(f, background=bkground)

        self.launches.create_index('launch_id', unique=True, background=bkground)
        self.launches.create_index('fw_id', background=bkground)
        self.launches.create_index('state_history.reservation_id', background=bkground)

        if GRIDFS_FALLBACK_COLLECTION is not None:
            files_collection = self.db["{}.files".format(GRIDFS_FALLBACK_COLLECTION)]
            files_collection.create_index('metadata.launch_id', unique=True, background=bkground)

        for f in ('state', 'time_start', 'time_end', 'host', 'ip', 'fworker.name'):
            self.launches.create_index(f, background=bkground)

        for f in ('name', 'created_on', 'updated_on', 'nodes'):
            self.workflows.create_index(f, background=bkground)

        for idx in self.user_indices:
            self.fireworks.create_index(idx, background=bkground)

        for idx in self.wf_user_indices:
            self.workflows.create_index(idx, background=bkground)

        # for frontend, which needs to sort on _id after querying on state
        self.fireworks.create_index([("state", DESCENDING), ("_id", DESCENDING)], background=bkground)
        self.fireworks.create_index([("state", DESCENDING), ("spec._priority", DESCENDING),
                                     ("created_on", DESCENDING)], background=bkground)
        self.fireworks.create_index([("state", DESCENDING), ("spec._priority", DESCENDING),
                                     ("created_on", ASCENDING)], background=bkground)
        self.workflows.create_index([("state", DESCENDING), ("_id", DESCENDING)], background=bkground)

        if not bkground:
            self.m_logger.debug('Compacting database...')
            try:
                self.db.command({'compact': 'fireworks'})
                self.db.command({'compact': 'launches'})
            except:
                self.m_logger.debug('Database compaction failed (not critical)')

    def _restart_ids(self, next_fw_id: int, next_launch_id: int):
        """
        internal method used to reset firework id counters.

        Args:
            next_fw_id (int): id to give next Firework
            next_launch_id (int): id to give next Launch
        """
        self.fw_id_assigner.delete_many({})
        self.fw_id_assigner.find_one_and_replace({'_id': -1},
                                                 {'next_fw_id': next_fw_id,
                                                  'next_launch_id': next_launch_id}, upsert=True)
        self.m_logger.debug(
            'RESTARTED fw_id, launch_id to ({}, {})'.format(next_fw_id, next_launch_id))

    def _get_a_fw_to_run(self, query: Dict=None, fw_id: int=None,
                         launch_idx: int=-1, checkout: bool=True) -> Firework:
        """
        Get the next ready firework to run.

        Args:
            query (dict)
            fw_id (int): If given the query is updated.
                Note: We want to return None if this specific FW  doesn't exist anymore. This is
                because our queue params might have been tailored to this FW.
            checkout (bool): if True, check out the matching firework and set state=RESERVED

        Returns:
            Firework
        """
        m_query = dict(query) if query else {}  # make a defensive copy
        m_query['state'] = 'READY'
        sortby = [("spec._priority", DESCENDING)]

        if SORT_FWS.upper() == "FIFO":
            sortby.append(("created_on", ASCENDING))
        elif SORT_FWS.upper() == "FILO":
            sortby.append(("created_on", DESCENDING))

        # Override query if fw_id defined
        if fw_id:
            m_query = {"fw_id": fw_id, "state": {'$in': ['READY', 'RESERVED']}}

        while True:
            # check out the matching firework, depending on the query set by the worker
            if checkout:
                m_fw = self.fireworks.find_one_and_update(m_query,
                                                          {'$set': {'state': 'RESERVED',
                                                           'updated_on': datetime.datetime.utcnow()}},
                                                          sort=sortby)
            else:
                m_fw = self.fireworks.find_one(m_query, {'fw_id': 1, 'spec': 1}, sort=sortby)

            if not m_fw:
                return None
            m_fw = self.get_fw_by_id(m_fw['fw_id'])
            if self._check_fw_for_uniqueness(m_fw):
                return m_fw

    def _get_active_launch_ids(self):
        """
        Get all the launch ids.

        Returns:
            list: all launch ids
        """
        all_launch_ids = []
        for l in self.fireworks.find({}, {"launches": 1}):
            all_launch_ids.extend(l['launches'])
        return all_launch_ids

    def _get_fw_ids_from_reservation_id(self, reservation_id: int) -> List[int]:
        """
        Given the reservation id, return the list of firework ids.

        Args:
            reservation_id (int)

        Returns:
            [int]: list of firework ids.
        """
        fw_ids = []
        ld = self.launches.find_one({"state_history.reservation_id": reservation_id},
                                      {'launch_id': 1})
        for fw in self.fireworks.find({'fw_id': ld['fw_id'],
                                        'launches': ld['launch_id']}, {'fw_id': 1}):
            fw_ids.append(fw['fw_id'])
        return fw_ids

    def _replace_fw(self, m_fw: Firework, state: str=None,
                    upsert: bool=False, fw_id: int=None):
        """
        Update a Firework m_fw in the database by replacing it.
        If upsert is True, add a new firework/launch if the no
        firework/launch has the same id
        """
        query = {'fw_id': fw_id or m_fw.fw_id}
        if type(state) == list:
            state = {'$in': state}
        if state:
            query['state'] = state
        fw_dict = m_fw.to_db_dict()
        launch = fw_dict.pop('launch')
        #if launch['launch_idx'] == -1:
        #    launch['launch_idx'] = self._get_next_launch_idx(m_fw.fw_id)
        launch_idx = launch.pop('launch_idx')
        
        if (launch_idx is not None) and (launch['action'] is None):
            # prevent too-large file from being uploaded.
            # might need to stop replacing the launch
            launches = self.fireworks.find_one(query, projection={'launches': 1})
            launch_ids = launches['launches'] if launches else []
            if launch_idx >= len(launch_ids):
                launch['launch_id'] = self.get_new_launch_id()
                launch_ids.append(launch['launch_id'])
            else:
                launch['launch_id'] = launch_ids[launch_idx]
            lquery = {'launch_id': launch['launch_id']}
            self.launches.find_one_and_replace(lquery, launch, upsert=upsert)
        else:
            launches = self.fireworks.find_one(query, projection={'launches': 1})
            launch_ids = launches['launches'] if launches else []
        fw_dict['launches'] = launch_ids
        fw = self.fireworks.find_one_and_replace(query, fw_dict, upsert=upsert)

    def _get_fw_ids_from_reservation_id(self, reservation_id: int) -> List[int]:
        fw_ids = []
        l_id = self.launches.find_one({"state_history.reservation_id": reservation_id},
                                  {'launch_id': 1})['launch_id']
        for fw in self.fireworks.find({'launches': l_id}, {'fw_id': 1}):
            fw_ids.append(fw['fw_id'])
        return fw_ids

    def _get_next_launch_idx(self, fw_id: int) -> int:
        return len(self.fireworks.find_one({'fw_id': fw_id}, projection=['launches'])['launches'])

    def _find_timeout_fws(self, state: str, expiration_secs: Num,
                          query: Dict=None) -> List[int]:
        now_time = datetime.datetime.utcnow()
        cutoff_timestr = (now_time - datetime.timedelta(seconds=expiration_secs)).isoformat()
        lostruns_query = {'state': state,
                          'state_history':
                              {'$elemMatch':
                                   {'state': state,
                                    'updated_on': {'$lte': cutoff_timestr}
                                    }
                               }
                          }

        if query:
            fw_ids = [x["fw_id"] for x in self.fireworks.find(query,
                                                          {"fw_id": 1})]
            lostruns_query["fw_id"] = {"$in": fw_ids}

        fw_ids = self.launches.find(lostruns_query, {'fw_id': 1})
        return list(set([id_dict['fw_id'] for id_dict in fw_ids]))

    def _get_lazy_firework(self, fw_id: int, launch_idx: int=-1) -> 'LazyFirework':
        return LazyFirework(fw_id, launch_idx, self.fireworks, self.launches, self.gridfs_fallback)
        #return self.get_fw_by_id(fw_id, launch_idx)

    def _find_wf(self, fw_id: int, projection: Dict=None,
                 sort: List[Tuple]=None) -> Dict:
        return self.workflows.find_one({'nodes': fw_id}, projection=projection, sort=sort)

    def _checkin_fw(self, m_fw: Firework, action: FWAction=None,
                    state: str='COMPLETED') -> Tuple[Dict, List[int]]:
        """
        Internal method used to mark a Firework's Launch as completed.

        Args:
            launch_id (int)
            action (FWAction): the FWAction of what to do next
            state (str): COMPLETED or FIZZLED

        Returns:
            dict: updated launch
        """
        # update the launch data to COMPLETED, set end time, etc
        m_launch = m_fw.launch
        launch_ids = self.fireworks.find_one({'fw_id': m_fw.fw_id},
                                            projection={'launches': 1})['launches']
        ids_to_refresh = []

        if (m_fw.launch_idx is not None):
            m_launch['launch_id'] = launch_ids[m_fw.launch_idx]
            try:
                self.launches.find_one_and_replace({'launch_id': m_launch['launch_id']},
                                                   m_launch, upsert=True)
            except DocumentTooLarge as err:
                launch_db_dict = m_launch
                action_dict = launch_db_dict.get("action", None)
                if not action_dict:
                    # in case the action is empty and it is not the source of
                    # the error, raise the exception again.
                    raise
                if self.gridfs_fallback is None:
                    err.args = (err.args[0]
                                + '. Set GRIDFS_FALLBACK_COLLECTION in FW_config.yaml'
                                  ' to a value different from None',)
                    raise err

                # encoding required for python2/3 compatibility.
                action_id = self.gridfs_fallback.put(json.dumps(action_dict), encoding="utf-8",
                                                     metadata={"launch_id": m_launch['launch_id']})
                launch_db_dict["action"] = {"gridfs_id": str(action_id)}
                self.m_logger.warning("The size of the launch document was too large. Saving "
                                   "the action in gridfs.")

                self.launches.find_one_and_replace({'launch_id': m_launch['launch_id']},
                                                   launch_db_dict, upsert=True)

            for fw in self.fireworks.find({'launches': m_launch['launch_id']}, {'fw_id': 1}):
                ids_to_refresh.append(fw['fw_id'])

        # change return type to dict to make return type serializable to support job packing
        return m_launch, ids_to_refresh

    def _update_fw(self, m_fw: Firework, state: str=None,
                   allowed_states: List[str]=None, launch_idx: int=-1,
                   touch_history: bool=True, checkpoint: Dict=None) -> Firework:
        # need to refine structure of launch_idx/launch_id arg to get correct id
        if type(m_fw) == int:
            m_fw = self.get_fw_by_id(m_fw, launch_idx)

        query_dict = {'fw_id': m_fw.fw_id}
        if type(allowed_states) == list:
            if m_fw.state not in allowed_states:
                return None
            allowed_states = {'$in': allowed_states}
        elif type(allowed_states) == str and (m_fw.state != allowed_states):
            return None
        if allowed_states is not None:
            query_dict['state'] = allowed_states

        reset_launch = False
        if state is not None:
            STATE_RANKS = Firework.STATE_RANKS
            reset_launch = STATE_RANKS[state] > STATE_RANKS[m_fw.state]\
                            and STATE_RANKS[m_fw.state] <= 0
        if touch_history and state is not None:
            m_fw.state = state
        
        launch = m_fw.launch
        command_dict_launch = {'$set': {'state_history': launch['state_history'],
                                 'trackers': [t for t in launch['trackers']]}}
        
        command_dict_fw = {}
        command_dict_fw['$set'] = {}
        command_dict_fw['$set']['state'] = m_fw.state
        command_dict_fw['$set']['updated_on'] = m_fw.updated_on
        if not reset_launch:
            lids = self.fireworks.find_one_and_update(query_dict, command_dict_fw,
                                            projection={'launches': 1})
            if lids and lids['launches']:
                lids = lids['launches']
                if launch_idx >= len(lids) or launch_idx < -len(lids):
                    raise ValueError("Bad launch index %d %d", launch_idx, len(lids))
                query_dict = {'launch_id': lids[launch_idx]}
                res = self.launches.update_one(query_dict, command_dict_launch)
        else:
            # make a new fw and a new launch by upserting with a new launch_idx
            # self._replace_fw(m_fw, upsert=True)
            # edit: should NOT make a new launch in this function
            self.fireworks.update_one(query_dict, command_dict_fw)
        return m_fw

    def get_new_fw_id(self, quantity: int=1) -> int:
        """
        Checkout the next Firework id

        Args:
            quantity (int): optionally ask for many ids, otherwise defaults to 1
                            this then returns the *first* fw_id in that range
        """
        try:
            return self.fw_id_assigner.find_one_and_update({}, {'$inc': {'next_fw_id': quantity}})['next_fw_id']
        except:
            raise ValueError("Could not get next FW id! If you have not yet initialized the database,"
                             " please do so by performing a database reset (e.g., lpad reset)")

    def get_new_launch_id(self):
        """
        Checkout the next Launch id
        """
        try:
            return self.fw_id_assigner.find_one_and_update({}, {'$inc': {'next_launch_id': 1}})['next_launch_id']
        except:
            raise ValueError("Could not get next launch id! If you have not yet initialized the "
                             "database, please do so by performing a database reset (e.g., lpad reset)")

    def _find_fws(self, fw_id: int=None, allowed_states: List[str]=None,
                  projection: Dict=None, sort: List[Tuple]=None,
                  m_query: Dict=None) -> List[Dict]:
        launch_sort=1
        query_dict = {}
        if sort == None:
            sort = []
        if fw_id is not None:
            if type(fw_id) == list:
                fw_id = {'$in': fw_id}
            query_dict['fw_id'] = fw_id
        if not (allowed_states is None):
            if type(allowed_states) == str:
                query_dict['state'] = allowed_states
            else:
                query_dict['state'] = {'$in': [allowed_states]}
        if not (m_query is None):
            query_dict.update(m_query)
        fws = self.fireworks.find(query_dict,
                                    projection=projection, sort=sort)
        sort.append(('launch_id', launch_sort))
        all_fws = []
        for fw in fws:
            query_dict = {'launch_id': {'$in': fw['launches']}}
            launches = self.launches.find(query_dict, sort=sort)
            for i, launch in enumerate(launches):
                launch['launch_idx'] = i
            query_dict['state'] = fw['state']
            if self.launches.count_documents(query_dict) == 0:
                print("NO LAUNCHES FOUND")
                new_fw = dict(fw)
                new_fw['launch'] = {}
                all_fws.append(new_fw)
                #raise ValueError('FW has no launch data!')
            for launch in launches:
                new_fw = dict(fw)
                new_fw['launch'] = launch
                all_fws.append(new_fw)
        return all_fws

    def _internal_fizzle(self, fw_id: int, launch_idx: int=-1):
        self.fireworks.find_one_and_update({"fw_id": fw_id},
                                            {"$set": {"state": "FIZZLED"}})
        self.workflows.find_one_and_update({"nodes": fw_id}, {"$set": {"state": "FIZZLED",\
                                           "fw_states.{}".format(fw_id): "FIZZLED"}})
        
    def _replace_fws(self, fw_ids: List[int], fws: List[Firework],
                     upsert: bool=False):
        self.fireworks.delete_many({'fw_id': {'$in': fw_ids}})
        for fw in fws:
            self._replace_fw(fw, upsert=upsert)
        """
        if type(fws) == Firework:
            fw_dict = fw.to_db_dict()
            launch = fw_dict.pop('launch')
            self.fireworks.insert_one(fw_dict)
            self.fireworks.insert_one(fw.to_db_dict())
        else:
            self.fireworks.insert_many(fw.to_db_dict() for fw in fws)
        """


    def _update_wf(self, wf: Workflow, updated_ids: List[int]):
        """
        Update the workflow with the update firework ids.
        Note: must be called within an enclosing WFLock

        Args:
            wf (Workflow)
            updated_ids ([int]): list of firework ids
        """
        updated_fws = [wf.id_fw[fid] for fid in updated_ids]
        old_new = self._upsert_fws(updated_fws)
        wf._reassign_ids(old_new)

        # find a node for which the id did not change, so we can query on it to get WF
        query_node = None
        for f in wf.id_fw:
            if f not in old_new.values() or old_new.get(f, None) == f:
                query_node = f
                break

        assert query_node is not None
        if not self.workflows.find_one({'nodes': query_node}):
            raise ValueError("BAD QUERY_NODE! {}".format(query_node))
        # redo the links and fw_states
        wf = wf.to_db_dict()
        wf['locked'] = True  # preserve the lock!
        self.workflows.find_one_and_replace({'nodes': query_node}, wf)

    def _steal_launches(self, thief_fw: Firework) -> bool:
        """
        Check if there are duplicates. If there are duplicates, the matching firework's launches
        are added to the launches of the given firework.

        Returns:
             bool: False if the given firework is unique
        """
        stolen = False
        if thief_fw.state in ['READY', 'RESERVED'] and '_dupefinder' in thief_fw.spec:
            m_dupefinder = thief_fw.spec['_dupefinder']
            # get the query that will limit the number of results to check as duplicates
            m_query = m_dupefinder.query(thief_fw.to_dict()["spec"])
            self.m_logger.debug('Querying for duplicates, fw_id: {}'.format(thief_fw.fw_id))
            # iterate through all potential duplicates in the DB
            for potential_match in self.fireworks.find(m_query):
                self.m_logger.debug('Verifying for duplicates, fw_ids: {}, {}'.format(
                    thief_fw.fw_id, potential_match['fw_id']))

                # see if verification is needed, as this slows the process
                verified = False
                try:
                    m_dupefinder.verify({}, {})  # is implemented test

                except NotImplementedError:
                    verified = True  # no dupefinder.verify() implemented, skip verification

                except:  # we want to catch any exceptions from testing an empty dict, which the dupefinder might not be designed for
                    pass

                if not verified:
                    # dupefinder.verify() is implemented, let's call verify()
                    spec1 = dict(thief_fw.to_dict()['spec'])  # defensive copy
                    spec2 = dict(potential_match['spec'])  # defensive copy
                    verified = m_dupefinder.verify(spec1, spec2)

                if verified:
                    # steal the launches
                    victim_fw = self.get_fw_by_id(potential_match['fw_id'])
                    thief_launches = [l.launch_id for l in thief_fw.launches]
                    valuable_launches = [l for l in victim_fw.launches if l.launch_id not in thief_launches]
                    for launch in valuable_launches:
                        thief_fw.launches.append(launch)
                        stolen = True
                        self.m_logger.info('Duplicate found! fwids {} and {}'.format(
                            thief_fw.fw_id, potential_match['fw_id']))
        return stolen

    def set_priority(self, fw_id: int, priority: int):
        """
        Set priority to the firework with the given id.

        Args:
            fw_id (int): firework id
            priority
        """
        self.fireworks.find_one_and_update({"fw_id": fw_id}, {'$set': {'spec._priority': priority}})

    def add_offline_run(self, fw_id: int, launch_idx: int=-1):
        """
        Add the launch and firework to the offline_run collection.

        Args:
            launch_id (int): launch id
            fw_id (id): firework id
            name (str)
        """
        #fw = self.get_fw_by_id(fw_id, launch_idx)
        # need to change this when fworker gets integrated
        fw = self.checkout_fw(os.getcwd(), fw_id, state='RESERVED')
        fw.state = "OFFLINE-RESERVED"
        fw.to_file("FW.json")
        with open('FW_offline.json', 'w') as f:
            f.write('{"fw_id":%d, "launch_id":%d}' % (fw_id,fw.launch_idx))
        self._replace_fw(fw)
        
    def recover_offline(self, fw_id: int, ignore_errors: bool=False,
                        print_errors: bool=False) -> Optional[int]:
        """
        Update the launch state using the offline data in FW_offline.json file.

        Args:
            launch_id (int): launch id
            ignore_errors (bool)
            print_errors (bool)

        Returns:
            firework id if the recovering fails otherwise None
        """
        # get the launch directory
        #m_launch = self.get_launch_by_fw_id(fw_id, -1)
        m_fw = self.get_fw_by_id(fw_id)
        try:
            self.m_logger.debug("RECOVERING fw_id: {}".format(m_fw.fw_id))
            # look for ping file - update the Firework if this is the case
            ping_loc = os.path.join(m_fw.launch_dir, "FW_ping.json")
            if os.path.exists(ping_loc):
                ping_dict = loadfn(ping_loc)
                self.ping_firework(fw_id, ptime=ping_dict['ping_time'])

            # look for action in FW_offline.json
            offline_loc = zpath(os.path.join(m_fw.launch_dir,
                                             "FW_offline.json"))
            with zopen(offline_loc) as f:
                offline_data = loadfn(offline_loc)
                if 'started_on' in offline_data:
                    m_fw.state = 'OFFLINE-RUNNING'
                    for s in m_fw.state_history:
                        if s['state'] == 'OFFLINE-RUNNING':
                            s['created_on'] = reconstitute_dates(offline_data['started_on'])
                    self._update_fw(m_fw)
                    #l = self.launches.find_one_and_replace({'launch_id': m_fw.launch},
                    #                                       m_launch.to_db_dict(), upsert=True)
                    #fw_id = l['fw_id']
                    #f = self.fireworks.find_one_and_update({'fw_id': fw_id},
                    #                                       {'$set':
                    #                                            {'state': 'RUNNING',
                    #                                             'updated_on': datetime.datetime.utcnow()
                    #                                             }
                    #                                        })
                    #if f:
                    self._refresh_wf(fw_id)

                # could cause file size problems doing this before checking for FWAction
                if 'checkpoint' in offline_data:
                    m_fw.touch_history(checkpoint=offline_data['checkpoint'])
                    self._update_fw(m_fw, touch_history=False)

                if 'fwaction' in offline_data:
                    fwaction = FWAction.from_dict(offline_data['fwaction'])
                    state = offline_data['state']
                    # start here
                    m_fw = Firework.from_dict(
                        self.checkin_fw(m_fw.fw_id, fwaction, state, m_fw.launch_idx))
                    for s in m_fw.state_history:
                        if s['state'] == offline_data['state']:
                            s['created_on'] = reconstitute_dates(offline_data['completed_on'])
                    #self.launches.find_one_and_update({'launch_id': m_fw.launch_id},
                    #                                  {'$set':
                    #                                       {'state_history': m_launch.state_history}
                    #                                  })
                    self._update_fw(m_fw, state=offline_data['state'], touch_history=False)
                    #self.offline_runs.update_one({"launch_id": launch_id},
                    #                             {"$set": {"completed": True}})

            # update the updated_on
            #self.offline_runs.update_one({"launch_id": launch_id},
            #                             {"$set": {"updated_on": datetime.datetime.utcnow().isoformat()}})
            return None
        except:
            if print_errors:
                self.m_logger.error("failed recovering fw_id {}-{}.\n{}".format(
                    m_fw.fw_id, m_fw.launch_idx, traceback.format_exc()))
            if not ignore_errors:
                traceback.print_exc()
                m_action = FWAction(stored_data={'_message': 'runtime error during task',
                                                 '_task': None,
                                                 '_exception': {'_stacktrace': traceback.format_exc(),
                                                                '_details': None}},
                                    exit=True)
                self.checkin_fw(m_fw.fw_id, m_action, 'FIZZLED', m_fw.launch_idx)
                #self.offline_runs.update_one({"launch_id": launch_id}, {"$set": {"completed": True}})
            return m_fw.fw_id

    def forget_offline(self, launchid_or_fwid: int, launch_mode: bool=True):
        """
        Unmark the offline run for the given launch or firework id.

        Args:
            launchid_or_fwid (int): launch od or firework id
            launch_mode (bool): if True then launch id is given.
        """
        q = {"launch_id": launchid_or_fwid} if launch_mode else {"fw_id": launchid_or_fwid}
        self.offline_runs.update_many(q, {"$set": {"deprecated": True}})

    def log_message(self, level: str, message: str):
        """
        Support for job packing

        Args:
            level (str)
            message (str)
        """
        self.m_logger.log(level, message)


class LazyFirework(object):
    """
    A LazyFirework only has the fw_id, and retrieves other data just-in-time.
    This representation can speed up Workflow loading as only "important" FWs need to be
    fully loaded.
    """

    # Get these fields from DB when creating new Firework object
    db_fields = ('name', 'fw_id', 'spec', 'created_on', 'updated_on', 'state')

    def __init__(self, fw_id: int, launch_idx: int,
                 fw_coll: Collection, launch_coll: Collection,
                 fallback_fs: Collection):
        """
        Args:
            fw_id (int): firework id
            fw_coll (pymongo.collection): fireworks collection
            launch_coll (pymongo.collection): launches collection
        """
        # This is the only attribute known w/o a DB query
        self.fw_id = fw_id
        self._launch_idx = launch_idx
        self._fwc, self._lc, self._ffs = fw_coll, launch_coll, fallback_fs
        self._launch = False
        self._fw, self._lids, self._state = None, None, None

    # Firework methods

    # Treat state as special case as it is always required when accessing a Firework lazily
    # If the partial fw is not available the state is fetched independently
    @property
    def state(self):
        if self._fw is not None:
            self._state = self._fw.state
        elif self._state is None:
            self._state = self._fwc.find_one({'fw_id': self.fw_id}, projection=['state'])['state']
        return self._state

    @state.setter
    def state(self, state: str):
        #self.partial_fw._state = state
        #self.partial_fw.updated_on = datetime.datetime.utcnow()
        self.full_fw.state = state

    def to_dict(self):
        return self.full_fw.to_dict()

    def _rerun(self):
        self.full_fw._rerun()

    def to_db_dict(self):
        return self.full_fw.to_db_dict()

    def __str__(self):
        return 'LazyFirework object: (id: {})'.format(self.fw_id)

    # Properties that shadow Firework attributes

    @property
    def tasks(self):
        return self.partial_fw.tasks

    @tasks.setter
    def tasks(self, value: List[Firetask]):
        self.partial_fw.tasks = value

    @property
    def spec(self):
        return self.partial_fw.spec

    @spec.setter
    def spec(self, value: Dict):
        self.partial_fw.spec = value

    @property
    def name(self):
        return self.partial_fw.name

    @name.setter
    def name(self, value: str):
        self.partial_fw.name = value

    @property
    def created_on(self):
        return self.partial_fw.created_on

    @created_on.setter
    def created_on(self, value: datetime.datetime):
        self.partial_fw.created_on = value

    @property
    def updated_on(self):
        return self.partial_fw.updated_on

    @updated_on.setter
    def updated_on(self, value: datetime.datetime):
        self.partial_fw.updated_on = value

    @property
    def parents(self):
        if self._fw is not None:
            return self.partial_fw.parents
        else:
            return []

    @parents.setter
    def parents(self, value: List[Firework]):
        self.partial_fw.parents = value

    # Properties that shadow Firework attributes, but which are
    # fetched individually from the DB (i.e. launch objects)

    @property
    def launch(self):
        return self._get_launch_data()

    @launch.setter
    def launch(self, value: Dict):
        self._launch = True
        self.full_fw.launch = value

    # Lazy properties that idempotently instantiate a Firework object
    @property
    def partial_fw(self):
        if not self._fw:
            fields = list(self.db_fields) + ['launches']
            data = self._fwc.find_one({'fw_id': self.fw_id}, projection=fields)
            launch_data = {}  # move some data to separate launch dict
            launch_data = data['launches']
            del data['launches']
            self._lids = launch_data
            self._fw = Firework.from_dict(data)
        return self._fw

    @property
    def full_fw(self):
        #map(self._get_launch_data, self.db_launch_fields)
        self._get_launch_data()
        return self._fw

    @property
    def action(self):
        return self.full_fw.action

    @property
    def trackers(self):
        return self.full_fw.trackers

    @property
    def host(self):
        return self.full_fw.host

    @property
    def ip(self):
        return self.full_fw.ip

    @property
    def fworker(self):
        return self.full_fw.fworker

    @property
    def state_history(self):
        return self.full_fw.state_history

    @property
    def launch_idx(self):
        self.partial_fw
        if self._launch_idx < 0:
            self._launch_idx = len(self._lids) + self._launch_idx
        return self._launch_idx

    # Get a type of Launch object

    def _get_launch_data(self):
        """
        Pull launch data individually for each field.

        Args:
            name (str): Name of field, e.g. 'archived_launches'.

        Returns:
            Launch obj (also propagated to self._fw)
        """
        fw = self.partial_fw  # assure stage 1
        if not self._launch:
            launch_ids = self._lids
            if launch_ids:
                launch_id = launch_ids[self._launch_idx]
                ld = self._lc.find_one({'launch_id': launch_id})
                ld["action"] = get_action_from_gridfs(ld.get("action"), self._ffs)
                ld.pop('launch_id')
                ld['launch_idx'] = self._launch_idx
                ld = _recursive_load(ld)

                fw._setup_launch(ld)  # put into real Firework obj
            else:
                fw._setup_launch({})
            self._launch = True
        return fw.launch


def get_action_from_gridfs(action_dict: Dict, fallback_fs: gridfs.GridFS):
    """
    Helper function to obtain the correct dictionary of the FWAction associated
    with a launch. If necessary retrieves the information from gridfs based
    on its identifier, otherwise simply returns the dictionary in input.
    Should be used when accessing a launch to ensure the presence of the
    correct action dictionary.
    
    Args:
        action_dict (dict): the dictionary contained in the "action" key of a launch
            document.
        fallback_fs (GridFS): the GridFS with the actions exceeding the 16MB limit.
    Returns:
        dict: the dictionary of the action.
    """

    if not action_dict or "gridfs_id" not in action_dict:
        return action_dict

    action_gridfs_id = ObjectId(action_dict["gridfs_id"])

    action_data = fallback_fs.get(ObjectId(action_gridfs_id))
    return json.loads(action_data.read().decode('utf-8'))