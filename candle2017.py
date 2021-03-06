#!/usr/bin/env python
# ----------------------------------------------------------------------------
# vim: ts=4:sw=4:et
# ----------------------------------------------------------------------------
# main.py
# ----------------------------------------------------------------------------

"""
Vela2017 entry point.
"""

import json
import os
import sys

from twisted.internet import task, defer
import wires

import player
import log
import inputs



def _load_settings(filename='settings.json'):

    """
    Returns a dict from the `filename` JSON contents.

    Updates the relative paths under levels.*.folder to absolute paths, assuming
    them to be relative to this module's directory.
    """

    # Load from this file's directory.
    base_dir = os.path.dirname(os.path.abspath(__file__))
    settings_fname = os.path.join(base_dir, filename)

    with open(settings_fname, 'rt') as f:
        settings = json.loads(f.read())

    # Update relative paths in settings with this file's directory.
    for level_info in settings['levels'].values():
        level_info_folder = level_info['folder']
        if not os.path.isabs(level_info_folder):
            level_info['folder'] = os.path.abspath(
                os.path.join(base_dir, level_info_folder)
            )

    return settings



@defer.inlineCallbacks
def _start_things(reactor, settings):

    """
    Asynchronous, Twisted based, main code.

    Sets up all needed objects, some dependent on the `settings` configuration
    dict, and starts the player manager.

    Exits when the player manager terminates.
    """

    # Setup the logging system.
    log_level = settings.get('loglevel', 'warn')
    log_levels = settings.get('loglevels', {})
    log.setup(level=log_level, namespace_levels=log_levels)


    # Create a call wiring object and tell it what to with `set_log_level` calls.
    wiring = wires.Wires()
    wiring.set_log_level.wire(log.set_level)


    # Create the input and player managers.
    input_manager = inputs.InputManager(reactor, wiring, settings)
    player_manager = player.PlayerManager(reactor, wiring, settings)

    # Both will be ayncrhronously started and stopped.
    startables = (input_manager, player_manager)


    # Before starting, ensure a clean stop.
    reactor.addSystemEventTrigger('before', 'shutdown', _stop_things, startables)


    # Start all things.
    for index, startable in enumerate(startables, start=1):
        try:
            yield startable.start()
        except Exception as e:
            # On failure logs should help diagnose.
            msg = 'Failed starting: %s\n' % e
            sys.stderr.write(msg.encode('utf-8'))
            raise SystemExit(-index)


    # Don't exit unless the player manager is ever done.
    yield player_manager.done



@defer.inlineCallbacks
def _stop_things(startables):

    """
    Asyncronous, Twisted based, cleanup.

    Asks each startable to stop.
    """

    for startable in startables:
        try:
            yield startable.stop()
        except Exception:
            # Nothing much we an do, move on.
            pass



def _main():

    """
    Main entry point.

    Loads settings, and drives the main asynchronous code.
    """

    settings = _load_settings()
    task.react(_start_things, (settings,))



if __name__ == '__main__':

    sys.exit(_main())


# ----------------------------------------------------------------------------
# main.py
# ----------------------------------------------------------------------------
