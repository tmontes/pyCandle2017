# ----------------------------------------------------------------------------
# vim: ts=4:sw=4:et
# ----------------------------------------------------------------------------
# player/player_manager.py
# ----------------------------------------------------------------------------

"""
Asyncronous, Twisted based, video playing interface.
"""

import collections
import os
import random

from twisted.internet import defer
from twisted import logger

from .dbus_manager import DBusManager
from .player import OMXPlayer



_log = logger.Logger(namespace='player.mngr')



class PlayerManager(object):

    """
    Tracks and controls video playing via one OMXPlayer instance per level.
    """

    # General lifecycle
    # -----------------
    # - Initialization finds available video files from the settings.
    # - Starting:
    #   - Spawns one OMXPlayer per level (which start in paused mode).
    #   - Unpause level 0 player.
    # - Level triggering calls (from the outside):
    #   - Unpause level X player.
    #   - Track player completion / process exit.
    #   - Respawn a new level X player.

    def __init__(self, reactor, wiring, settings):
        """
        Initializes the player manager:
        - `reactor` is the Twisted reactor.
        - `wiring` is used to register `change_play_level` handling.
        - `settings` is a dict with:
           - ['environment']['ld_library_path']
           - ['environment']['omxplayer_bin']
           - ['levels'][*]['folder']
           - ['levels'][*]['fadein']
           - ['levels'][*]['fadeout']
        """
        self._wiring = wiring
        self._settings = settings

        # part of our public interface, OMXPlayer will use this
        self.reactor = reactor
        self.dbus_mgr = DBusManager(reactor, settings)

        # keys/values: integer levels/queue of video files
        self._files = {}

        # keys/values: integer levels/list of OMXPlayer instances
        self._players = collections.defaultdict(collections.deque)

        self._base_player = None
        self._current_player = None     # if not level 0
        self._current_level = None

        self._update_ld_lib_path()
        self._find_files()

        self._stopping = False
        self.done = defer.Deferred()


    def _update_ld_lib_path(self):

        # Spawning omxplayer.bin requires associated shared libraries.
        # This updates/extends the LD_LIBRARY_PATH linux environment variable
        # such that those libraries can be found.

        extra_ld_lib_path = self._settings['environment']['ld_library_path']
        if extra_ld_lib_path:
            ld_lib_path = os.environ.get('LD_LIBRARY_PATH', '')
            if ld_lib_path:
                new_ld_lib_path = '%s:%s' % (extra_ld_lib_path, ld_lib_path)
            else:
                new_ld_lib_path = extra_ld_lib_path
            os.environ['LD_LIBRARY_PATH'] = new_ld_lib_path
            _log.debug('LD_LIBRARY_PATH set to {v!r}', v=new_ld_lib_path)


    def _find_files(self):

        # Populate self._files by finding available files under each
        # level's configured folder in the settings dict.

        for level, level_info in self._settings['levels'].items():
            level_folder = level_info['folder']
            self._files[int(level)] = [
                os.path.join(level_folder, name)
                for name in os.listdir(level_folder)
            ]
        _log.debug('files found: {d!r}', d=self._files)


    def _get_file_for_level(self, level):

        # Return a random filename from the available files in `level`.

        return random.sample(self._files[level], 1)[0]


    @property
    def executable(self):
        """
        Part of the public API, used by OMXPlayer.
        """
        return self._settings['environment']['omxplayer_bin']


    @defer.inlineCallbacks
    def start(self):
        """
        Spawns one player per level ensuring the level 0 one is playing.
        """
        _log.info('starting')

        yield self.dbus_mgr.connect_to_dbus(disconnect_callable=self._dbus_disconnected)

        self._base_player = yield self._create_player(level=0)
        yield self._base_player.play()
        self._current_level = 0

        yield self._create_players()

        # Ready to respond to change level requests.
        self._wiring.change_play_level.wire(self._change_play_level)

        _log.info('started')


    @defer.inlineCallbacks
    def _dbus_disconnected(self):

        # Called by DBus manager when DBus connection is lost.

        if self._stopping:
            return

        _log.warn('lost DBus connection: stopping')
        yield self.stop(skip_dbus=True)
        _log.warn('lost DBus connection: stopped')


    @defer.inlineCallbacks
    def _create_player(self, level):

        # Spawns a player with a random video file for the given `level`.
        # Ensures:
        # - Level 0 players loop.
        # - Player end is tracked.

        _log.info('creating player level={l!r}', l=level)
        player = OMXPlayer(
            self._get_file_for_level(level),
            self,
            layer=level,
            alpha=0,
            loop=(level == 0),
            fadein=self._settings['levels'][str(level)]['fadein'],
            fadeout=self._settings['levels'][str(level)]['fadeout'],
        )
        yield player.spawn(end_callable=lambda _: self._player_ended(player, level))
        return player


    @defer.inlineCallbacks
    def _create_players(self):

        # Populate or re-populate self._players.

        for level in range(1, 4):
            need_player_count = 2 if level != 3 else 1
            have_player_count = len(self._players[level])
            for _ in range(need_player_count-have_player_count):
                player = yield self._create_player(level)
                self._players[level].append(player)


    def _get_player(self, level):

        # Return the next player for level.

        return self._players[level].popleft()


    def _change_play_level(self, new_level, comment=''):
        """
        Triggers video playing level change.
        Does nothing if:
        - Startup isn't completed.
        - `new_level` is less than or equal to the currently running level.
        """

        _log.info('new_level={l!r} comment={c!r}', l=new_level, c=comment)

        if new_level == 0:
            _log.info('will not go to rest ahead of time')
            return

        if self._current_level == 3:
            _log.info('will not override level 3 player')
            return

        if new_level >= self._current_level:
            retrigger = new_level == self._current_level
            new_player = self._get_player(level=new_level)
            new_player.play(skip_fadein=retrigger)
            if self._current_player:
                if retrigger:
                    _log.info('re-triggering level {l!r}', l=new_level)
                    self._current_player.stop()
                else:
                    _log.debug('smoothly stopping current player')
                    self._current_player.fadeout_and_stop()
            else:
                _log.debug('no current player to smoothly stop')
            self._current_player = new_player
            self._current_level = new_level
            _log.debug('current player updated')
            return

        _log.info('will not trigger a lower level')


    @defer.inlineCallbacks
    def _player_ended(self, player, level):

        # Called when a player for `level` ends (see _create_player), ensures
        # that new player is created such that it is ready when needed.

        _log.info('player level={l!r} ended', l=level)
        if self._stopping:
            return
        if player in self._players[level]:
            _log.warn('process ended unexpectedly')
            self._players[level].remove(player)
        yield self._create_players()
        if player is self._current_player:
            _log.debug('current player set to none')
            self._current_player = None
        self._current_level = 0


    @defer.inlineCallbacks
    def stop(self, skip_dbus=False):
        """
        Asks all players to stop (IOW: terminate) and exits cleanly.
        """
        if self._stopping:
            return

        _log.info('stopping')

        self._stopping = True
        self._wiring.change_play_level.unwire(self._change_play_level)
        for level, players in self._players.items():
            while players:
                _log.info('stopping player level={l!r}', l=level)
                yield players.popleft().stop(skip_dbus)
                _log.info('stopped player level={l!r}', l=level)
        
        _log.info('stopping base player')
        yield self._base_player.fadeout_and_stop()
        _log.info('stopped base player')

        _log.info('cleaning up dbus manager')
        yield self.dbus_mgr.cleanup()
        _log.info('cleaned up dbus manager')

        _log.info('stopped')
        self.done.callback(None)


# ----------------------------------------------------------------------------
# player/player_manager.py
# ----------------------------------------------------------------------------
