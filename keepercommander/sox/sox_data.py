import logging
from typing import Iterable, Dict, Set, List

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from . import sox_types, sqlite_storage, storage_types
from .sox_types import RecordPermissions


class RebuildTask:
    def __init__(self, is_full_sync, load_compliance_data=False):      # type: (bool, bool) -> None
        self.is_full_sync = is_full_sync                    # type: bool
        self.load_compliance_data = load_compliance_data    # type: bool
        self.records = set()                                # type: Set[str]

    def update_records(self, record_ids):    # type: (Iterable[str]) -> None
        if self.is_full_sync:
            return
        self.records.update(record_ids)


class SoxData:
    def __init__(self, ec_private_key, storage):
        # type: (EllipticCurvePrivateKey, sqlite_storage.SqliteSoxStorage) -> None
        self.ec_private_key = ec_private_key    # type: EllipticCurvePrivateKey
        self.storage = storage                  # type: sqlite_storage.SqliteSoxStorage
        self._records = {}                      # type: Dict[str, sox_types.Record]
        self._users = {}                        # type: Dict[int, sox_types.EnterpriseUser]
        self._teams = {}                        # type: Dict[str, sox_types.Team]
        self._shared_folders = {}               # type: Dict[str, sox_types.SharedFolder]
        task = RebuildTask(True)
        self.rebuild_data(task)

    def get_records(self, record_ids=None):
        return self._records if record_ids is None else {uid: self._records.get(uid) for uid in record_ids}

    def get_user(self, uid):    # type: (int) -> sox_types.EnterpriseUser
        return self._users.get(uid)

    def get_users(self, user_ids=None):
        return self._users if user_ids is None else {uid: self.get_user(uid) for uid in user_ids}

    def get_team(self, team_uid):
        return self._teams.get(team_uid)

    def get_teams(self, team_uids=None):
        return self._teams if team_uids is None else {uid: self.get_team(uid) for uid in team_uids}

    def get_user_records(self, user_uids=None):
        users = self._users.values() if user_uids is None else {self.get_user(uid) for uid in user_uids}
        recs = set()
        for user in users:
            for r_uid in user.records:
                recs.add(sox_types.UserRecord(user.user_uid, self._records.get(r_uid)))
        return recs

    def get_shared_folders(self, sf_uids=None):
        return self._shared_folders if sf_uids is None else {uid: self._shared_folders.get(uid) for uid in sf_uids}

    @property
    def record_count(self):   # type: () -> int
        return len(self._records)

    def rebuild_data(self, changes):   # type: (RebuildTask) -> None
        def link_record_permissions(store, record_lookup):
            links = store.get_record_permissions().get_all_links()
            for link in links:
                record = record_lookup.get(link.record_uid)
                if record:
                    record.user_permissions.update({link.user_uid: link.permissions})
                else:
                    logging.info(f'record (uid = {link.record_uid}) not found')

            return record_lookup

        def load_records(store, changes):
            # type: (sqlite_storage.SqliteSoxStorage, RebuildTask) -> Dict[str, sox_types.Record]
            entities = []   # type: List[storage_types.StorageRecord]
            if changes.records:
                for uid in changes.records:
                    entity = store.records.get_entity(uid)
                    if entity:
                        entities.append(entity)
            else:
                entities.extend(store.records.get_all())

            record_lookup = {}
            for entity in entities:
                try:
                    record = sox_types.Record.load(entity, self.ec_private_key)
                    if record:
                        record_lookup[record.record_uid] = record
                except:
                    logging.debug('Cannot decrypt record \"%s\" info.', entity.record_uid)
            record_lookup = link_record_aging(store, record_lookup)
            return link_record_permissions(store, record_lookup) if changes.load_compliance_data else record_lookup

        def link_record_aging(store, record_lookup):
            for aging in store.record_aging.get_all():
                record = record_lookup.get(aging.record_uid)
                if record:
                    record.created = aging.created
                    record.last_pw_change = aging.last_pw_change
            return record_lookup

        def link_user_records(store, user_lookup):
            links = store.get_user_record_links().get_all_links()
            for link in links:
                user = user_lookup.get(link.user_uid)
                if user:
                    user.records.append(link.record_uid)
                else:
                    logging.info(f'user (uid = {link.user_uid} not found')
            return user_lookup

        def load_users(store):  # type: (sqlite_storage.SqliteSoxStorage) -> Dict[int, sox_types.EnterpriseUser]
            users = [sox_types.EnterpriseUser.load(eu) for eu in store.users.get_all()]
            u_lookup = {user.user_uid: user for user in users}
            return link_user_records(store, u_lookup)

        def load_teams(store):
            teams = [sox_types.Team.load(entity) for entity in store.teams.get_all()]
            team_lookup = {team.team_uid: team for team in teams}
            return link_team_users(store, team_lookup)
            # return update_team_users(store, link_sf_teams(store, team_lookup))

        def link_team_users(store, team_lookup):
            links = store.get_team_user_links().get_all_links()
            for link in links:
                team = team_lookup.get(link.team_uid)
                team.users.append(link.user_uid)
            return team_lookup

        def load_sf_records(store):
            links = store.get_sf_record_links().get_all_links()
            folder_lookup = dict()
            for link in links:
                folder = folder_lookup.get(link.folder_uid) or sox_types.SharedFolder.load(link)
                if folder.record_permissions[-1].record_uid != link.record_uid:
                    folder.record_permissions.append(RecordPermissions(link.record_uid, link.permissions))
                folder_lookup[link.folder_uid] = folder

            folder_lookup = link_sf_users(store, folder_lookup)
            return link_sf_teams(store, folder_lookup)

        def link_sf_users(store, sf_lookup):
            links = store.get_sf_user_links().get_all_links()
            for link in links:
                folder = sf_lookup.get(link.folder_uid)
                folder.users.append(link.user_uid)
            return sf_lookup

        def link_sf_teams(store, sf_lookup):
            links = store.get_sf_team_links().get_all_links()
            for link in links:
                folder = sf_lookup.get(link.folder_uid)
                folder.teams.append(link.team_uid)
            return sf_lookup

        if changes.load_compliance_data:
            self._teams.update(load_teams(self.storage))
            self._shared_folders.update(load_sf_records(self.storage))
        self._records.update(load_records(self.storage, changes))
        if changes.is_full_sync or changes.load_compliance_data:
            self._users.update(load_users(self.storage))