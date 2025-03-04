# -*- coding: utf-8 -*-
# Copyright European Organization for Nuclear Research (CERN) since 2012
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Judge-Injector is a daemon to asynchronously create replication rules
"""
import functools
import logging
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from random import randint
from re import match

from sqlalchemy.exc import DatabaseError

import rucio.db.sqla.util
from rucio.common.logging import setup_logging
from rucio.common.exception import (DatabaseException, RuleNotFound, RSEWriteBlocked,
                                    ReplicationRuleCreationTemporaryFailed, InsufficientAccountLimit)
from rucio.core.monitor import record_counter
from rucio.core.rule import inject_rule, get_injected_rules, update_rule
from rucio.daemons.common import run_daemon

graceful_stop = threading.Event()


def rule_injector(once=False, sleep_time=60):
    """
    Main loop to check for asynchronous creation of replication rules
    """
    executable = 'judge-injector'
    paused_rules = {}  # {rule_id: datetime}
    run_daemon(
        once=once,
        graceful_stop=graceful_stop,
        executable=executable,
        logger_prefix=executable,
        partition_wait_time=1,
        sleep_time=sleep_time,
        run_once_fnc=functools.partial(
            run_once,
            paused_rules=paused_rules,
        )
    )


def run_once(paused_rules, heartbeat_handler, **_kwargs):
    worker_number, total_workers, logger = heartbeat_handler.live()

    try:
        start = time.time()

        # Refresh paused rules
        iter_paused_rules = deepcopy(paused_rules)
        for key in iter_paused_rules:
            if datetime.utcnow() > paused_rules[key]:
                del paused_rules[key]

        rules = get_injected_rules(total_workers=total_workers,
                                   worker_number=worker_number,
                                   limit=100,
                                   blocked_rules=[key for key in paused_rules])
        logger(logging.DEBUG, 'index query time %f fetch size is %d' % (time.time() - start, len(rules)))

        if not rules:
            logger(logging.DEBUG, 'did not get any work (paused_rules=%s)' % str(len(paused_rules)))
            return

        for rule in rules:
            _, _, logger = heartbeat_handler.live()
            rule_id = rule[0]
            logger(logging.INFO, 'Injecting rule %s' % rule_id)
            if graceful_stop.is_set():
                break
            try:
                start = time.time()
                inject_rule(rule_id=rule_id, logger=logger)
                logger(logging.DEBUG, 'injection of %s took %f' % (rule_id, time.time() - start))
            except (DatabaseException, DatabaseError) as e:
                if match('.*ORA-00054.*', str(e.args[0])):
                    paused_rules[rule_id] = datetime.utcnow() + timedelta(seconds=randint(60, 600))
                    record_counter('rule.judge.exceptions.{exception}', labels={'exception': 'LocksDetected'})
                    logger(logging.WARNING, 'Locks detected for %s' % rule_id)
                elif match('.*QueuePool.*', str(e.args[0])):
                    logger(logging.WARNING, 'DatabaseException', exc_info=True)
                    record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
                elif match('.*ORA-03135.*', str(e.args[0])):
                    logger(logging.WARNING, 'DatabaseException', exc_info=True)
                    record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
                else:
                    logger(logging.ERROR, 'DatabaseException', exc_info=True)
                    record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
            except (RSEWriteBlocked) as e:
                paused_rules[rule_id] = datetime.utcnow() + timedelta(seconds=randint(60, 600))
                logger(logging.WARNING, 'RSEWriteBlocked for rule %s' % rule_id)
                record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
            except ReplicationRuleCreationTemporaryFailed as e:
                paused_rules[rule_id] = datetime.utcnow() + timedelta(seconds=randint(60, 600))
                logger(logging.WARNING, 'ReplicationRuleCreationTemporaryFailed for rule %s' % rule_id)
                record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
            except RuleNotFound:
                pass
            except InsufficientAccountLimit:
                # A rule with InsufficientAccountLimit on injection hangs there potentially forever
                # It should be marked as SUSPENDED
                logger(logging.INFO, 'Marking rule %s as SUSPENDED due to InsufficientAccountLimit' % rule_id)
                update_rule(rule_id=rule_id, options={'state': 'SUSPENDED'})

    except (DatabaseException, DatabaseError) as e:
        if match('.*QueuePool.*', str(e.args[0])):
            logger(logging.WARNING, 'DatabaseException', exc_info=True)
            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
        elif match('.*ORA-03135.*', str(e.args[0])):
            logger(logging.WARNING, 'DatabaseException', exc_info=True)
            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
        else:
            logger(logging.CRITICAL, 'DatabaseException', exc_info=True)
            record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})
    except Exception as e:
        logger(logging.CRITICAL, 'Exception', exc_info=True)
        record_counter('rule.judge.exceptions.{exception}', labels={'exception': e.__class__.__name__})


def stop(signum=None, frame=None):
    """
    Graceful exit.
    """

    graceful_stop.set()


def run(once=False, threads=1, sleep_time=60):
    """
    Starts up the Judge-Injector threads.
    """
    setup_logging()

    if rucio.db.sqla.util.is_old_db():
        raise DatabaseException('Database was not updated, daemon won\'t start')

    if once:
        rule_injector(once)
    else:
        logging.info('Injector starting %s threads' % str(threads))
        threads = [threading.Thread(target=rule_injector, kwargs={'once': once,
                                                                  'sleep_time': sleep_time}) for i in range(0, threads)]
        [t.start() for t in threads]
        # Interruptible joins require a timeout.
        while threads[0].is_alive():
            [t.join(timeout=3.14) for t in threads]
