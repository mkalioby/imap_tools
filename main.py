import re
import email
import imaplib
from typing import Iterator, Iterable
from email.header import decode_header
import imap_utf7

# Maximal line length when calling readline(). This is to prevent reading arbitrary length lines.
imaplib._MAXLINE = 4 * 1024 * 1024  # 4Mb


class BaseImapToolsError(Exception):
    """Base exception"""


# todo
def _check_ok_status(command, typ, data):
    """
    Check that command responses status equals expected status
    If not, raises BaseImapToolsError
    """
    if typ != 'OK':
        raise BaseImapToolsError(
            'Response status for command "{command}" = "{typ}", "OK" expected, data: {data}'.format(
                command=command, typ=typ, data=str(data)))


def _quote(arg):
    if isinstance(arg, str):
        return '"' + arg.replace('\\', '\\\\').replace('"', '\\"') + '"'
    else:
        return b'"' + arg.replace(b'\\', b'\\\\').replace(b'"', b'\\"') + b'"'


def _pairs_to_dict(items: list) -> dict:
    """Example: ['MESSAGES', '3', 'UIDNEXT', '4'] -> {'MESSAGES': '3', 'UIDNEXT': '4'}"""
    if len(items) % 2 != 0:
        raise ValueError('An even-length array is expected')
    return dict((items[i * 2], items[i * 2 + 1]) for i in range(len(items) // 2))


class MailBox(object):
    """Working with the email box throught IMAP"""

    # UID parse rules
    pattern_uid_re_set = [
        re.compile('\(UID (?P<uid>\d+) RFC822'),  # zimbra, yandex, gmail
        re.compile('(?P<uid>\d+) \(RFC822'),  # icewarp
    ]

    # standard mail message flags
    standard_rw_flags = ('SEEN', 'ANSWERED', 'FLAGGED', 'DELETED', 'DRAFT')

    class MailBoxSearchError(BaseImapToolsError):
        """Search error"""

    class MailBoxWrongFlagError(BaseImapToolsError):
        """Wrong flag for "flag" method"""

    class MailBoxUidParamError(BaseImapToolsError):
        """Wrong uid param"""

    def __init__(self, *args):
        self.box = imaplib.IMAP4_SSL(*args)

    def login(self, username: str, password: str, initial_folder: str = 'INBOX'):
        self._username = username
        self._password = password
        self._initial_folder = initial_folder
        self.box.login(self._username, self._password)
        self.folder = MailFolderManager(self.box)
        self.folder.set(self._initial_folder)

    def logout(self):
        self.box.logout()

    @staticmethod
    def _parse_uid(data: bytes) -> str or None:
        """Parse email uid"""
        for pattern_uid_re in MailBox.pattern_uid_re_set:
            uid_match = pattern_uid_re.search(data.decode())
            if uid_match:
                return uid_match.group('uid')
        return None

    @staticmethod
    def _clean_message_data(data):
        """
        :param data: Message object model
        Get message data and uid data
        *Elements may contain byte strings in any order, like: b'4517 (FLAGS (\\Recent NonJunk))'
        """
        message_data = b''
        uid_data = b''
        for i in range(len(data)):
            # miss trash
            if type(data[i]) is bytes and b'(FLAGS' in data[i]:
                continue
            # data, uid
            if type(data[i]) is tuple:
                message_data = data[i][1]
                uid_data = data[i][0]

        return message_data, uid_data

    def fetch(self, search_criteria: str = 'ALL', limit: int = None, miss_defect=True) -> Iterator[object]:
        """
        Mail message generator in current folder by search criteria
        :param search_criteria: Message search criteria (see examples at ./doc/imap_search_criteria.txt)
        :param limit: limit on the number of read emails
        :param miss_defect: miss defect emails
        """
        typ, data = self.box.search(None, search_criteria)
        if typ != 'OK':
            raise self.MailBoxSearchError('{0}: {1}'.format(typ, str(data)))
        # first element is string with email numbers through the gap
        for i, message_id in enumerate(data[0].decode().split(' ') if data[0] else ()):
            if limit and i >= limit:
                break
            # get message by id
            typ, data = self.box.fetch(message_id, "(RFC822 UID)")  # *RFC-822 - format of the mail message
            message_data, uid_data = self._clean_message_data(data)
            message_obj = email.message_from_bytes(message_data)
            if message_obj:
                if miss_defect and message_obj.defects:
                    continue
                yield MailMessage(message_id, self._parse_uid(uid_data), message_obj)

    @staticmethod
    def _uid_str(uid_list: Iterable[str]) -> str:
        """Prepare list of uid for use in commands: delete/copy/move/seen"""
        if not uid_list:
            raise MailBox.MailBoxUidParamError('uid_list should be not empty')
        if type(uid_list) is str:
            raise MailBox.MailBoxUidParamError('uid_list can not be str')
        return ','.join(uid_list)

    def expunge(self) -> tuple:
        return self.box.expunge()

    def delete(self, uid_list: [str]) -> tuple:
        """Delete email messages"""
        store_result = self.box.uid('STORE', self._uid_str(uid_list), '+FLAGS', '(\Deleted)')
        expunge_result = self.expunge()
        return store_result, expunge_result

    def copy(self, uid_list: [str], destination_folder: str) -> tuple:
        """Copy email messages into the specified folder"""
        return self.box.uid('COPY', self._uid_str(uid_list), destination_folder)

    def move(self, uid_list: [str], destination_folder: str) -> tuple:
        """Move email messages into the specified folder"""
        uid_arg = self._uid_str(uid_list)
        copy_result = self.copy(uid_arg, destination_folder)
        delete_result = self.delete(uid_arg)
        return copy_result, delete_result

    def flag(self, uid_list: [str], flag_set: [str], value: bool) -> tuple:
        """
        Change email flag
        Typical flags contains in MailBox.standard_rw_flags
        """
        for flag_name in flag_set:
            if flag_name.upper() not in self.standard_rw_flags:
                raise self.MailBoxWrongFlagError('Unsupported flag: {}'.format(flag_name))
        store_result = self.box.uid(
            'STORE', self._uid_str(uid_list), ('+' if value else '-') + 'FLAGS',
            '({})'.format(' '.join(('\\' + i for i in flag_set))))
        expunge_result = self.expunge()
        return store_result, expunge_result

    def seen(self, uid_list: [str], seen_val: bool) -> tuple:
        """
        Mark email as read/unread
        This is shortcut for flag method
        """
        return self.flag(uid_list, 'Seen', seen_val)


class MailMessage(object):
    """The email message"""

    def __init__(self, msg_id: str, msg_uid: str, msg_obj: 'email.message.Message'):
        self.id = msg_id
        self.uid = msg_uid
        self.obj = msg_obj

    @staticmethod
    def _decode_value(value, encoding):
        """Converts value to utf-8 encoding"""
        if isinstance(value, bytes):
            if encoding in ['utf-8', None]:
                return value.decode('utf-8', 'ignore')
            else:
                return value.decode(encoding)
        return value

    @property
    def subject(self) -> str:
        """Message subject"""
        if 'subject' in self.obj:
            msg_subject = decode_header(self.obj['subject'])
            return self._decode_value(msg_subject[0][0], msg_subject[0][1])
        return ''

    @staticmethod
    def _parse_email_address(address: str) -> dict:
        """
        Parse email address str, example: "Ivan Petrov" <ivan@mail.ru>
        @:return dict(name: str, email: str, full: str)
        """
        result = dict(email='', name='', full='')
        if '<' in address and '>' in address:
            match = re.match('(?P<name>.*)?(?P<email><.*>)', address, re.UNICODE)
            result['name'] = match.group('name').strip()
            result['email'] = match.group('email').strip('<>')
            result['full'] = address
        else:
            result['name'] = ''
            result['email'] = result['full'] = address.strip()
        return result

    @property
    def from_values(self) -> dict:
        """The address of the sender (all data)"""
        from_header_cleaned = re.sub('[\n\r\t]+', ' ', self.obj['from'])
        msg_from = decode_header(from_header_cleaned)
        msg_txt = ''.join(self._decode_value(part[0], part[1]) for part in msg_from)
        return self._parse_email_address(msg_txt)

    @property
    def from_(self) -> str:
        """The address of the sender"""
        return self.from_values['email']

    @property
    def to_values(self) -> list:
        """The addresses of the recipients (all data)"""
        if 'to' in self.obj:
            msg_to = decode_header(self.obj['to'])
            return [self._parse_email_address(part) for part in
                    self._decode_value(msg_to[0][0], msg_to[0][1]).split(',')]
        return []

    @property
    def to(self) -> list:
        """The addresses of the recipients"""
        return [i['email'] for i in self.to_values]

    @property
    def date(self) -> str:
        """Message date"""
        return str(self.obj['Date'] or '')

    @property
    def text(self) -> str or None:
        """The text of the mail message"""
        for part in self.obj.walk():
            # multipart/* are just containers
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get_content_type() in ('text/plain', 'text/'):
                return part.get_payload(decode=True).decode('utf-8', 'ignore')
        return None

    @property
    def html(self) -> str or None:
        """HTML text of the mail message"""
        for part in self.obj.walk():
            # multipart/* are just containers
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get_content_type() == 'text/html':
                return part.get_payload(decode=True).decode('utf-8', 'ignore')
        return None

    def get_attachments(self) -> Iterator(str, bytes):
        """
        Attachments of the mail message (generator)
        :return: Iterator(filename: str, payload: bytes)
        """
        for part in self.obj.walk():
            # multipart/* are just containers
            if part.get_content_maintype() == 'multipart':
                continue
            if part.get('Content-Disposition') is None:
                continue
            filename = part.get_filename()
            if not part.get_filename():
                continue  # this is what happens when Content-Disposition = inline
            filename = self._decode_value(*decode_header(filename)[0])
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            yield filename, payload


class MailFolderManager(object):
    """Operations with mail box folders"""

    folder_status_options = ['MESSAGES', 'RECENT', 'UIDNEXT', 'UIDVALIDITY', 'UNSEEN']

    class MailBoxFolderSetError(BaseImapToolsError):
        """Wrong folder name error"""

    class MailBoxFolderWrongStatusError(BaseImapToolsError):
        """Wrong folder name error"""

    def __init__(self, box):
        self.box = box

    def _normalise_folder(self, folder):
        """Normalise folder name"""
        if isinstance(folder, bytes):
            folder = folder.decode('ascii')
        return _quote(imap_utf7.encode(folder))

    def set(self, folder):
        """Select current folder"""
        result = self.box.select(folder)
        if result[0] != 'OK':
            raise self.MailBoxFolderSetError(result[1])

    def get(self):
        pass

    def create(self):
        pass

    def rename(self):
        pass

    def delete(self):
        pass

    def status(self, folder: str, options: [str] or None = None):
        """
        Get the status of a folder
        :param folder: mailbox folder
        :param options: [str] with values from MailFolderManager.folder_status_options or None,
                by default - get all options
            MESSAGES - The number of messages in the mailbox.
            RECENT - The number of messages with the \Recent flag set.
            UIDNEXT - The next unique identifier value of the mailbox.
            UIDVALIDITY - The unique identifier validity value of the mailbox.
            UNSEEN - The number of messages which do not have the \Seen flag set.
        :return: dict with options keys
        """
        command = 'STATUS'
        if not options:
            options = self.folder_status_options
        if not all([i in self.folder_status_options for i in options]):
            raise self.MailBoxFolderWrongStatusError(str(options))
        typ, data = self.box._simple_command(command, self._normalise_folder(folder), '({})'.format(' '.join(options)))
        _check_ok_status(command, typ, data)
        typ, data = self.box._untagged_response(typ, data, command)
        _check_ok_status(command, typ, data)
        values = data[0].decode().split('(')[1].split(')')[0].split(' ')
        return _pairs_to_dict(values)

    def list(self, folder: str = '""', search_args: str = '*', subscribed_only: bool = False):
        """
        Get a listing of folders on the server
        :param folder: mailbox folder, if empty list shows all content from root
        :param search_args: search argumets, is case-sensitive mailbox name with possible wildcards
            * is a wildcard, and matches zero or more characters at this position
            % is similar to * but it does not match a hierarchy delimiter
        :param subscribed_only: bool - get only subscribed folders
        :return: dict(
            flags: str - folder flags,
            delim: str - delimitor,
            name: str - folder name,
        )
        """
        folder_item_re = re.compile(r'\((?P<flags>[\S ]*)\) "(?P<delim>[\S ]+)" "(?P<name>[\S ]+)"')
        command = 'LSUB' if subscribed_only else 'LIST'
        typ, data = self.box._simple_command(command, self._normalise_folder(folder), search_args)
        typ, data = self.box._untagged_response(typ, data, command)
        result = list()
        for folder_item in data:
            if not folder_item:
                continue
            folder_match = re.search(folder_item_re, imap_utf7.decode(folder_item))
            result.append(folder_match.groupdict())
        return result
