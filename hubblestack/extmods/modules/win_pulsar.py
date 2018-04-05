#win_notify
'''
This will setup your computer to enable auditing for specified folders inputted into a yaml file. It will
then scan the ntfs journal for changes to those folders and report when it finds one.
'''


from __future__ import absolute_import
from time import mktime, strptime, time

import collections
import datetime
import fnmatch
import logging
import os
import yaml

import salt.ext.six
import salt.loader
import salt.utils.platform

log = logging.getLogger(__name__)
DEFAULT_MASK = ['File create', 'File delete', 'Hard link change', 'Data extend', 
                'Data overwrite', 'Data truncation', 'Security change']

__virtualname__ = 'pulsar'
CONFIG = None
CONFIG_STALENESS = 0


def __virtual__():
    if not salt.utils.platform.is_windows():
        return False, 'This module only works on windows'
    win_version = __grains__['osfullname']
    if '2012' not in win_version and '2016' not in win_version:
        return False, 'This module only works with Server 2012 (Win8) or higher'
    return __virtualname__

def process(configfile='salt://hubblestack_pulsar/hubblestack_pulsar_win_config.yaml',
            verbose=False):
    '''
    Watch the configured files

    Example yaml config on fileserver (targeted by configfile option)

    .. code-block:: yaml
        C:\Users: {}
        C:\Windows:
          mask:
            - 'File Create'
            - 'File Delete'
            - 'Security Change'
          exclude:
            - C:\Windows\System32\*
        C:\temp: {}
        return: splunk_pulsar_return
        batch: True

    Note that if 'batch: True', the configured returner must support receiving a list of events, rather than single one-off events

    the mask list can contain the following events (the default mask is create, delete, and modify):

        1.  Basic Info Change                A user has either changed file or directory attributes, or one or more time stamps
        2.  Close                            The file or directory is closed
        3.  Compression Change               The compression state of the file or directory is changed from or to compressed
        4.  Data Extend                      The file or directory is extended (added to)
        5.  Data Overwrite                   The data in the file or directory is overwritten
        6.  Data Truncation                  The file or directory is truncated
        7.  EA Change                        A user made a change to the extended attributes of a file or directory (These NTFS 
                                                    file system attributes are not accessible to Windows-based applications)
        8.  Encryption Change                The file or directory is encrypted or decrypted
        9.  File Create                      The file or directory is created for the first time
        10. File Delete                      The file or directory is deleted
        11. Hard Link Change                 An NTFS file system hard link is added to or removed from the file or directory
        12. Indexable Change                 A user changes the FILE_ATTRIBUTE_NOT_CONTENT_INDEXED attribute (changes the file 
                                                    or directory from one where content can be indexed to one where content cannot 
                                                    be indexed, or vice versa)
        13. Integrity Change                 A user changed the state of the FILE_ATTRIBUTE_INTEGRITY_STREAM attribute for the given 
                                                    stream (On the ReFS file system, integrity streams maintain a checksum of all 
                                                    data for that stream, so that the contents of the file can be validated during 
                                                    read or write operations)
        14. Named Data Extend                The one or more named data streams for a file are extended (added to)
        15. Named Data Overwrite             The data in one or more named data streams for a file is overwritten
        16. Named Data truncation            The one or more named data streams for a file is truncated
        17. Object ID Change                 The object identifier of a file or directory is changed
        18. Rename New Name                  A file or directory is renamed, and the file name in the USN_RECORD_V2 structure is the 
                                                    new name
        19. Rename Old Name                  The file or directory is renamed, and the file name in the USN_RECORD_V2 structure is
                                                    the previous name
        20. Reparse Point Change             The reparse point that is contained in a file or directory is changed, or a reparse 
                                                    point is added to or deleted from a file or directory
        21. Security Change                  A change is made in the access rights to a file or directory
        22. Stream Change                    A named stream is added to or removed from a file, or a named stream is renamed
        23. Transacted Change                The given stream is modified through a TxF transaction

    exclude:
        Exclude directories or files from triggering events in the watched directory. **Note that the directory excludes shoud
        not have a trailing slash**
    
    :return:
    '''
    config = __salt__['config.get']('hubblestack_pulsar' , {})
    if isinstance(configfile, list):
        config['paths'] = configfile
    else:
        config['paths'] = [configfile]
    config['verbose'] = verbose
    global CONFIG_STALENESS
    global CONFIG
    if config.get('verbose'):
        log.debug('Pulsar module called.')
        log.debug('Pulsar module config from pillar:\n{0}'.format(config))
    ret = []
    sys_check = 0

    # Get config(s) from filesystem if we don't have them already
    if CONFIG and CONFIG_STALENESS < config.get('refresh_frequency', 60):
        CONFIG_STALENESS += 1
        CONFIG.update(config)
        CONFIG['verbose'] = config.get('verbose')
        config = CONFIG
    else:
        if config.get('verbose'):
            log.debug('No cached config found for pulsar, retrieving fresh from fileserver.')
        new_config = config
        if isinstance(config.get('paths'), list):
            for path in config['paths']:
                if 'salt://' in path:
                    path = __salt__['cp.cache_file'](path)
                if os.path.isfile(path):
                    with open(path, 'r') as f:
                        new_config = _dict_update(new_config,
                                                  yaml.safe_load(f),
                                                  recursive_update=True,
                                                  merge_lists=True)
                else:
                    log.error('Path {0} does not exist or is not a file'.format(path))
        else:
            log.error('Pulsar beacon \'paths\' data improperly formatted. Should be list of paths')

        new_config.update(config)
        config = new_config
        CONFIG_STALENESS = 0
        CONFIG = config

    if config.get('verbose'):
        log.debug('Pulsar beacon config (compiled from config list):\n{0}'.format(config))

    # check if cache path contails starting point for 'fsutil usn readjournal'
    cache_path = os.path.join(__opts__['cachedir'], 'win_pulsar_usn')
    # if starting point doesn't exist, create one then finish until next run
    if not os.path.isfile(cache_path):
        qj_dict = queryjournal('C:')
        with open(cache_path, 'w') as f:
            f.write(qj_dict['Next Usn'])
        return ret

    # check if file is out of date
    currentt = time()
    file_mtime = os.path.getmtime(cache_path)
    threshold = int(__opts__.get('file_threshold', 900))
    th_check = currentt - threshold
    if th_check > file_mtime:
        qj_dict = queryjournal('C:')
        with open(cache_path, 'w') as f:
            f.write(qj_dict['Next Usn'])
        return ret

    # read in start location and grab all changes since then
    with open(cache_path, 'r') as f:
        nusn = f.read()
    nusn, jitems = readjournal('C:', nusn)

    # create new starting point for next run
    with open(cache_path, 'w') as f:
        f.write(nusn)

    # filter out unrequested changed
    ret_list = usnfilter(jitems, config)

    # return list of dictionaries
    return ret_list
    
        
def queryjournal(drive):
    '''
    Gets information on the journal prosiding on the drive passed into the method
    returns a dictionary with the following information:
      USN Journal ID
      First USN of the journal
      Next USN to be written to the journal
      Lowest Valid USN of the journal since the biginning of the volume (this will most likely 
                                  not be in the current journal since it only keeys a few days)
      Max USN of the journal (the highest number reachable for a single Journal)
      Maximum Size
      Allocation Delta
      Minimum record version supported
      Maximum record version supported
      Write range tracking (enabled or disabled)
    '''
    qjournal =  (__salt__['cmd.run']('fsutil usn queryjournal {0}'.format(drive))).split('\r\n')
    qj_dict = {}
    #format into dictionary
    if qjournal:
        #remove empty string
        qjournal.pop()
        for item in qjournal:
            qkey, qvalue = item.split(': ')
            qj_dict[qkey.strip()] = qvalue.strip()
    return qj_dict

def readjournal(drive, next_usn=0):
    '''
    Reads the data inside the journal.  Default is to start from the beginning, 
    but you can pass an argument to start from whichever usn you want
    Returns a list of dictionaries with the following information
      list:
        Individual events
    
      dictionary:
        Usn Journal ID (event number)
        File Name
        File name Length
        Reason (what hapened to the file)
        Time Stamp
        File attributes
        File ID
        Parent file ID
        Source Info
        Security ID
        Major version
        Minor version
        Record length
    '''
    jdata = (__salt__['cmd.run']('fsutil usn readjournal {0} startusn={1}'.format(drive, next_usn))).split('\r\n\r\n')
    jd_list = []
    pattern = '%m/%d/%Y %H:%M:%S'
    removable = {'File name length', 'Major version', 'Minor version', 'Record length', 'Security ID', 'Source info'}
    if jdata:
        #prime for next delivery
        jinfo = jdata[0].split('\r\n')
        nusn = jinfo[2].split(' : ')[1]
        #remove first item of list
        jdata.pop(0)
        #format into dictionary
        for dlist in jdata:
            jd_dict = {}
            i_list = dlist.split('\r\n')
            for item in i_list:
                if item == '':
                    continue
                dkey, dvalue = item.split(' : ')
                if dkey.strip() in removable:
                    continue
                if dkey.strip() == 'Time stamp':
                    dvalue = int(mktime(strptime(dvalue.strip(), pattern)))
                    jd_dict[dkey.strip()] = dvalue
                else:
                    jd_dict[dkey.strip()] = dvalue.strip()
            jd_dict['Full path'] = getfilepath(jd_dict['File ID'], jd_dict['Parent file ID'], jd_dict['File name'], drive)
            del jd_dict['File ID'], jd_dict['Parent file ID']
            jd_list.append(jd_dict)
    return nusn, jd_list


def getfilepath(fid, pfid, fname, drive):
    '''
    Gets file name and path from a File ID
    '''
    try:
        jfullpath = (__salt__['cmd.run']('fsutil file queryfilenamebyid {0} 0x{1}'.format(drive, fid), ignore_retcode=True)).replace('?\\', '\r\n')
    except:
        log.debug('Current usn item is not a file')
        return None
    
    if 'Error:' in jfullpath:
        log.debug('Searching for the File ID came back with error.  Trying the parent folder')
        jfullpath = (__salt__['cmd.run']('fsutil file queryfilenamebyid {0} 0x{1}'.format(drive, pfid), ignore_retcode=True)).replace('?\\', '\r\n')
        if 'Error:' in jfullpath:
            log.debug('Current usn cannot be queried as file')
            return None
        retpath = jfullpath.split('\r\n')[1] + '\\' + fname
        return retpath
    retpath = jfullpath.split('\r\n')[1]
    return retpath

def usnfilter(usn_list, config_paths):
    '''
    Iterates through each change in the list and throws out any change not specified in the win_pulsar.yaml
    '''
    ret_usns = []
    basic_paths = [] 

    # iterate through active portion of the NTFS change journal
    for usn in usn_list:
        # iterate through win_pulsar.yaml (skips all non file paths)
        for path in config_paths:
            if path in {'win_notify_interval', 'return', 'batch', 'checksum', 'stats', 'paths', 'verbose'}:
                continue
            if not os.path.exists(path):
                log.info('the folder path {} does not exist'.format(path))
                continue
        
            if isinstance(config_paths[path], dict):
                mask = config_paths[path].get('mask', DEFAULT_MASK)
                recurse = config_paths[path].get('recurse', True)
                exclude = config_paths[path].get('exclude', False)
                sum_type = config_paths[path].get('checksum', 'sha256')
            else:
                mask = DEFAULT_MASK
                recurse = True
                exclude = False

            fpath = usn['Full path']
            if fpath is None:
                log.debug('The following change made was not a file. {0}'.format(usn))
                continue
            # check if base path called out in yaml is in file location called out in actual change
            if path in fpath:
                #check if the type of change that happened matches the list in yaml
                freason = usn['Reason'].split(': ')[1]
                freason = freason.split('|')[0].strip()
                if freason in mask:
                    throw_away = False
                    if exclude is not False:
                        for p in exclude:
                            # fnmatch allows for * and ? as wildcards
                            if fnmatch.fnmatch(fpath, p):
                                throw_away = True
                                # if the path matches a path we don't care about, stop iterating through excludes
                                break
                    if throw_away is True:
                        # stop iterating through win_pulsar specified paths since throw away flag was set
                        break
                    else:
                        usn['checksum'] = get_file_hash(fpath, sum_type)
                        usn['checksum_type'] = sum_type
                        usn['tag'], _ = os.path.split(fpath)
                        ret_usns.append(usn)
                    # don't keep checking other paths in yaml since we already found a match
                    break
                else:
                    continue
                # don't keep checking other paths in yaml since we already found a match
                break
            else:
                continue
    return ret_usns

def get_file_hash(usn_file, checksum):
    '''
    Simple function to grab the hash for each file that has been flagged
    '''
    try:
        hashy = __salt__['file.get_hash']('{0}'.format(usn_file), form=checksum)
        return hashy
    except:
        return ''

def canary(change_file=None):
    '''
    Simple module to change a file to trigger a FIM event (daily, etc)

    THE SPECIFIED FILE WILL BE CREATED AND DELETED

    Defaults to CONF_DIR/fim_canary.tmp, i.e. /etc/hubble/fim_canary.tmp
    '''
    if change_file is None:
        conf_dir = os.path.dirname(__opts__['conf_file'])
        change_file = os.path.join(conf_dir, 'fim_canary.tmp')
    __salt__['file.touch'](change_file)
    os.remove(change_file)

def _dict_update(dest, upd, recursive_update=True, merge_lists=False):
    '''
    Recursive version of the default dict.update

    Merges upd recursively into dest

    If recursive_update=False, will use the classic dict.update, or fall back
    on a manual merge (helpful for non-dict types like FunctionWrapper)

    If merge_lists=True, will aggregate list object types instead of replace.
    This behavior is only activated when recursive_update=True. By default
    merge_lists=False.
    '''
    if (not isinstance(dest, collections.Mapping)) \
            or (not isinstance(upd, collections.Mapping)):
        raise TypeError('Cannot update using non-dict types in dictupdate.update()')
    updkeys = list(upd.keys())
    if not set(list(dest.keys())) & set(updkeys):
        recursive_update = False
    if recursive_update:
        for key in updkeys:
            val = upd[key]
            try:
                dest_subkey = dest.get(key, None)
            except AttributeError:
                dest_subkey = None
            if isinstance(dest_subkey, collections.Mapping) \
                    and isinstance(val, collections.Mapping):
                ret = _dict_update(dest_subkey, val, merge_lists=merge_lists)
                dest[key] = ret
            elif isinstance(dest_subkey, list) \
                     and isinstance(val, list):
                if merge_lists:
                    dest[key] = dest.get(key, []) + val
                else:
                    dest[key] = upd[key]
            else:
                dest[key] = upd[key]
        return dest
    else:
        try:
            for k in upd.keys():
                dest[k] = upd[k]
        except AttributeError:
            # this mapping is not a dict
            for k in upd:
                dest[k] = upd[k]
        return dest


def top(topfile='salt://hubblestack_pulsar/win_top.pulsar',
        verbose=False):

    configs = get_top_data(topfile)

    configs = ['salt://hubblestack_pulsar/' + config.replace('.','/') + '.yaml'
               for config in configs]

    return process(configs, verbose=verbose)


def get_top_data(topfile):

    topfile = __salt__['cp.cache_file'](topfile)

    try:
        with open(topfile) as handle:
            topdata = yaml.safe_load(handle)
    except Exception as e:
        raise CommandExecutionError('Could not load topfile: {0}'.format(e))

    if not isinstance(topdata, dict) or 'pulsar' not in topdata or \
            not(isinstance(topdata['pulsar'], dict)):
        raise CommandExecutionError('Pulsar topfile not formatted correctly')

    topdata = topdata['pulsar']

    ret = []

    for match, data in topdata.iteritems():
        if __salt__['match.compound'](match):
            ret.extend(data)

    return ret
