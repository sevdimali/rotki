import tempfile
import time
import os
import shutil
from collections import defaultdict
from pysqlcipher3 import dbapi2 as sqlcipher

from rotkehlchen.constants import SUPPORTED_EXCHANGES
from rotkehlchen.utils import ts_now
from rotkehlchen.errors import AuthenticationError, InputError
from .utils import DB_SCRIPT_CREATE_TABLES, DB_SCRIPT_REIMPORT_DATA

DEFAULT_START_DATE = "01/08/2015"
DEFAULT_UI_FLOATING_PRECISION = 2
KDF_ITER = 64000


def str_to_bool(s):
    return True if s == 'True' else False


ROTKEHLCHEN_DB_VERSION = 1


# https://stackoverflow.com/questions/4814167/storing-time-series-data-relational-or-non
# http://www.sql-join.com/sql-join-types
class DBHandler(object):

    def __init__(self, user_data_dir, username, password):
        self.user_data_dir = user_data_dir
        self.connect(password)
        try:
            self.conn.executescript(DB_SCRIPT_CREATE_TABLES)
        except sqlcipher.DatabaseError:
            raise AuthenticationError('Wrong password while decrypting the database')

        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT OR IGNORE INTO settings(name, value) VALUES(?, ?)',
            ('version', str(ROTKEHLCHEN_DB_VERSION))
        )
        self.conn.commit()

    def connect(self, password):
        self.conn = sqlcipher.connect(os.path.join(self.user_data_dir, 'rotkehlchen.db'))
        self.conn.text_factory = str
        self.conn.executescript('PRAGMA key="{}"; PRAGMA kdf_iter={};'.format(password, KDF_ITER))
        self.conn.execute('PRAGMA foreign_keys=ON')

    def disconnect(self):
        self.conn.close()

    def reimport_all_tables(self):
        """Useful only when some table's column data type was modified and you
        need to re-import all data. Should only be used if you know what you are
        doing. For normal database upgrades the proper scripts should be used"""
        self.conn.executescript(DB_SCRIPT_REIMPORT_DATA)

    def export_unencrypted(self, temppath):
        self.conn.executescript(
            'ATTACH DATABASE "{}" AS plaintext KEY "";'
            'SELECT sqlcipher_export("plaintext");'
            'DETACH DATABASE plaintext;'.format(temppath)
        )

    def import_unencrypted(self, unencrypted_db_data, password):
        self.disconnect()
        rdbpath = os.path.join(self.user_data_dir, 'rotkehlchen.db')
        # Make copy of existing encrypted DB before removing it
        shutil.copy2(
            rdbpath,
            os.path.join(self.user_data_dir, 'rotkehlchen_temp_backup.db')
        )
        os.remove(rdbpath)

        # dump the unencrypted data into a temporary file
        with tempfile.TemporaryDirectory() as tmpdirname:
            tempdbpath = os.path.join(tmpdirname, 'temp.db')
            with open(tempdbpath, 'wb') as f:
                f.write(unencrypted_db_data)

            # Now attach to the unencrypted DB and copy it to our DB and encrypt it
            self.conn = sqlcipher.connect(tempdbpath)
            self.conn.executescript(
                'ATTACH DATABASE "{}" AS encrypted KEY "{}";'
                'PRAGMA encrypted.kdf_iter={};'
                'SELECT sqlcipher_export("encrypted");'
                'DETACH DATABASE encrypted;'.format(rdbpath, password, KDF_ITER)
            )
            self.disconnect()

        self.connect(password)
        # all went okay, remove the original temp backup
        os.remove(os.path.join(self.user_data_dir, 'rotkehlchen_temp_backup.db'))

    def update_last_write(self):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
            ('last_write_ts', str(ts_now()))
        )
        self.conn.commit()

    def get_last_write_ts(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM settings where name=?;', ('last_write_ts',)
        )
        query = query.fetchall()
        # If setting is not set, it's 0 by default
        if len(query) == 0:
            return 0
        return int(query[0][0])

    def update_last_data_upload_ts(self):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
            ('last_data_upload_ts', str(ts_now()))
        )
        self.conn.commit()

    def get_last_data_upload_ts(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM settings where name=?;', ('last_data_upload_ts',)
        )
        query = query.fetchall()
        # If setting is not set, it's 0 by default
        if len(query) == 0:
            return 0
        return int(query[0][0])

    def update_premium_sync(self, should_sync):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
            ('premium_should_sync', str(should_sync))
        )
        self.conn.commit()

    def get_premium_sync(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM settings where name=?;', ('premium_should_sync',)
        )
        query = query.fetchall()
        # If setting is not set, it's false by default
        if len(query) == 0:
            return False
        return str_to_bool(query[0])

    def get_settings(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT name, value FROM settings;'
        )
        query = query.fetchall()

        settings = {}
        for q in query:
            if q[0] == 'version':
                settings['db_version'] = int(q[1])
            elif q[0] == 'last_write_ts':
                settings['last_write_ts'] = int(q[1])
            elif q[0] == 'premium_should_sync':
                settings['premium_should_sync'] = str_to_bool(q[1])
            elif q[0] == 'last_data_upload_ts':
                settings['last_data_upload_ts'] = int(q[1])
            elif q[0] == 'ui_floating_precision':
                settings['ui_floating_precision'] = int(q[1])
            else:
                settings[q[0]] = q[1]

        # Populate defaults for values not in the DB yet
        if 'historical_data_start' not in settings:
            settings['historical_data_start'] = DEFAULT_START_DATE
        if 'eth_rpc_port' not in settings:
            settings['eth_rpc_port'] = '8545'
        if 'ui_floating_precision' not in settings:
            settings['ui_floating_precision'] = DEFAULT_UI_FLOATING_PRECISION
        return settings

    def get_main_currency(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM settings WHERE name="main_currency";'
        )
        query = query.fetchall()
        if len(query) == 0:
            return 'USD'
        return query[0][0]

    def set_main_currency(self, currency):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
            ('main_currency', currency)
        )
        self.conn.commit()
        self.update_last_write()

    def set_settings(self, settings):
        cursor = self.conn.cursor()
        cursor.executemany(
            'INSERT OR REPLACE INTO settings(name, value) VALUES(?, ?)',
            [setting for setting in list(settings.items())]
        )
        self.conn.commit()
        self.update_last_write()

    def add_to_ignored_assets(self, asset):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT INTO multisettings(name, value) VALUES(?, ?)',
            ('ignored_asset', asset)
        )
        self.conn.commit()

    def remove_from_ignored_assets(self, asset):
        cursor = self.conn.cursor()
        cursor.execute(
            'DELETE FROM multisettings WHERE name="ignored_asset" AND value=?;',
            (asset,)
        )
        self.conn.commit()

    def get_ignored_assets(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM multisettings WHERE name="ignored_asset";'
        )
        query = query.fetchall()
        return [q[0] for q in query]

    def add_multiple_balances(self, balances):
        """Execute addition of multiple balances in the DB

        balances should be a list of tuples each containing:
        (time, asset, amount, usd_value)"""
        cursor = self.conn.cursor()
        cursor.executemany(
            'INSERT INTO timed_balances('
            '    time, currency, amount, usd_value) '
            ' VALUES(?, ?, ?, ?)',
            balances
        )
        self.conn.commit()
        self.update_last_write()

    def add_multiple_location_data(self, location_data):
        """Execute addition of multiple location data in the DB

        location_data should be a list of tuples each containing:
        (time, location, usd_value)"""
        cursor = self.conn.cursor()
        cursor.executemany(
            'INSERT INTO timed_location_data('
            '    time, location, usd_value) '
            ' VALUES(?, ?, ?)',
            location_data
        )
        self.conn.commit()
        self.update_last_write()

    def write_owned_tokens(self, tokens):
        """Execute addition of multiple tokens in the DB

        tokens should be a list of token symbols
        (time, location, usd_value)"""
        cursor = self.conn.cursor()
        # Delete previous list and write the new one
        cursor.execute(
            'DELETE FROM multisettings WHERE name="eth_token";'
        )
        cursor.executemany(
            'INSERT INTO multisettings(name,value) VALUES (?, ?)',
            [('eth_token', t) for t in tokens]
        )
        self.conn.commit()
        self.update_last_write()

    def get_owned_tokens(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT value FROM multisettings WHERE name="eth_token";'
        )
        query = query.fetchall()
        return [q[0] for q in query]

    def add_blockchain_account(self, blockchain, account):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT INTO blockchain_accounts(blockchain, account) VALUES (?, ?)',
            (blockchain, account)
        )
        self.conn.commit()
        self.update_last_write()

    def remove_blockchain_account(self, blockchain, account):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT COUNT(*) from blockchain_accounts WHERE '
            'blockchain = ? and account = ?;', (blockchain, account)
        )
        query = query.fetchall()
        if query[0][0] == 0:
            raise InputError(
                'Tried to remove non-existing {} account {}'.format(blockchain, account)
            )

        cursor.execute(
            'DELETE FROM blockchain_accounts WHERE '
            'blockchain = ? and account = ?;', (blockchain, account)
        )
        self.conn.commit()
        self.update_last_write()

    def add_fiat_balance(self, currency, amount):
        cursor = self.conn.cursor()
        # We don't care about previous value so this simple insert or replace should work
        cursor.execute(
            'INSERT OR REPLACE INTO current_balances(asset, amount) VALUES (?, ?)',
            (currency, amount)
        )
        self.conn.commit()
        self.update_last_write()

    def remove_fiat_balance(self, currency):
        cursor = self.conn.cursor()
        cursor.execute(
            'DELETE FROM current_balances WHERE asset = ?;', (currency,)
        )
        self.conn.commit()
        self.update_last_write()

    def get_fiat_balances(self):
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT asset, amount FROM current_balances;'
        )
        query = query.fetchall()

        result = {}
        for entry in query:
            result[entry[0]] = entry[1]
        return result

    def get_blockchain_accounts(self):
        """Returns a dictionary with keys being blockchains and values being
        lists of accounts"""
        cursor = self.conn.cursor()
        query = cursor.execute(
            'SELECT blockchain, account FROM blockchain_accounts;'
        )
        query = query.fetchall()
        result = defaultdict(list)

        for entry in query:
            if entry[0] not in result:
                result[entry[0]] = []

            result[entry[0]].append(entry[1])

        return result

    def remove(self):
        cursor = self.conn.cursor()
        cursor.execute('DROP TABLE IF EXISTS timed_balances')
        cursor.execute('DROP TABLE IF EXISTS timed_location_data')
        cursor.execute('DROP TABLE IF EXISTS timed_unique_data')
        self.conn.commit()

    def write_balances_data(self, data):
        ts = int(time.time())
        balances = []
        locations = []

        for key, val in data.items():
            if key in ('location', 'net_usd'):
                continue

            balances.append((
                ts,
                key,
                str(val['amount']),
                str(val['usd_value']),
            ))

        for key, val in data['location'].items():
            locations.append((
                ts, key, str(val['usd_value'])
            ))
        locations.append((ts, 'total', str(data['net_usd'])))

        self.add_multiple_balances(balances)
        self.add_multiple_location_data(locations)

    def add_exchange(self, name, api_key, api_secret):
        if name not in SUPPORTED_EXCHANGES:
            raise InputError('Unsupported exchange {}'.format(name))

        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT INTO user_credentials (name, api_key, api_secret) VALUES (?, ?, ?)',
            (name, api_key, api_secret)
        )
        self.conn.commit()
        self.update_last_write()

    def remove_exchange(self, name):
        cursor = self.conn.cursor()
        cursor.execute(
            'DELETE FROM user_credentials WHERE name =?', (name,)
        )
        self.conn.commit()
        self.update_last_write()

    def get_exchange_secrets(self):
        cursor = self.conn.cursor()
        result = cursor.execute(
            'SELECT name, api_key, api_secret FROM user_credentials;'
        )
        result = result.fetchall()
        secret_data = {}
        for entry in result:
            if entry == 'rotkehlchen':
                continue
            name = entry[0]
            secret_data[name] = {
                'api_key': str(entry[1]),
                'api_secret': str(entry[2])
            }

        return secret_data

    def add_external_trade(
            self,
            time,
            location,
            pair,
            trade_type,
            amount,
            rate,
            fee,
            fee_currency,
            link,
            notes
    ):
        cursor = self.conn.cursor()
        cursor.execute(
            'INSERT INTO trades('
            '  time,'
            '  location,'
            '  pair,'
            '  type,'
            '  amount,'
            '  rate,'
            '  fee,'
            '  fee_currency,'
            '  link,'
            '  notes)'
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (
                time,
                location,
                pair,
                trade_type,
                amount,
                rate,
                fee,
                fee_currency,
                link,
                notes
            )
        )
        self.conn.commit()

    def edit_external_trade(
            self,
            trade_id,
            time,
            location,
            pair,
            trade_type,
            amount,
            rate,
            fee,
            fee_currency,
            link,
            notes
    ):
        cursor = self.conn.cursor()
        cursor.execute(
            'UPDATE trades SET '
            '  time=?,'
            '  location=?,'
            '  pair=?,'
            '  type=?,'
            '  amount=?,'
            '  rate=?,'
            '  fee=?,'
            '  fee_currency=?,'
            '  link=?,'
            '  notes=? '
            'WHERE id=?',
            (
                time,
                location,
                pair,
                trade_type,
                amount,
                rate,
                fee,
                fee_currency,
                link,
                notes,
                trade_id,
            )
        )
        if cursor.rowcount == 0:
            return False, 'Tried to edit non existing external trade id'

        self.conn.commit()
        return True, ''

    def get_external_trades(self, from_ts=None, to_ts=None):
        cursor = self.conn.cursor()
        query = (
            'SELECT id,'
            '  time,'
            '  location,'
            '  pair,'
            '  type,'
            '  amount,'
            '  rate,'
            '  fee,'
            '  fee_currency,'
            '  link,'
            '  notes FROM trades WHERE location="external" '
        )
        bindings = ()
        if from_ts:
            query += 'AND time >= ? '
            bindings = (from_ts,)
            if to_ts:
                query += 'AND time <= ? '
                bindings = (from_ts, to_ts,)
        elif to_ts:
            query += 'AND time <= ? '
            bindings = (to_ts,)
        query += 'ORDER BY time ASC;'
        results = cursor.execute(query, bindings)
        results = results.fetchall()

        trades = []
        for result in results:
            trades.append({
                'id': result[0],
                # At the moment all trades have "timestamp" and not time
                'timestamp': result[1],
                'location': result[2],
                'pair': result[3],
                'type': result[4],
                'amount': result[5],
                'rate': result[6],
                'fee': result[7],
                'fee_currency': result[8],
                'link': result[9],
                'notes': result[10],
            })

        return trades

    def delete_external_trade(self, trade_id):
        cursor = self.conn.cursor()
        cursor.execute('DELETE FROM trades WHERE id=?', (trade_id,))
        if cursor.rowcount == 0:
            return False, 'Tried to delete non-existing external trade'
        self.conn.commit()
        return True, ''

    def set_rotkehlchen_premium(self, api_key, api_secret):
        cursor = self.conn.cursor()
        # We don't care about previous value so this simple insert or replace should work
        cursor.execute(
            'INSERT OR REPLACE INTO user_credentials(name, api_key, api_secret) VALUES (?, ?, ?)',
            ('rotkehlchen', api_key, api_secret)
        )
        self.conn.commit()
        # Do not update the last write here. If we are starting in a new machine
        # then this write is mandatory and to sync with data from server we need
        # an empty last write ts in that case
        # self.update_last_write()

    def get_rotkehlchen_premium(self):
        cursor = self.conn.cursor()
        result = cursor.execute(
            'SELECT api_key, api_secret FROM user_credentials where name="rotkehlchen";'
        )
        result = result.fetchall()
        if len(result) == 1:
            return result[0]
        else:
            return None
