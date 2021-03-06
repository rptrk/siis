# @date 2018-08-07
# @author Frederic SCHERMA
# @license Copyright (c) 2018 Dream Overflow
# Desktop notification handler

import collections
import threading
import os
import time
import logging
import subprocess
import traceback

from importlib import import_module
from datetime import datetime, timedelta

from trader.position import Position
from monitor.discord.webhooks import send_to_discord

from notifier.notifiable import Notifiable
from notifier.signal import Signal

from terminal.terminal import Terminal
from database.database import Database

from common.utils import timeframe_to_str

import logging
logger = logging.getLogger('siis.monitor.desktopnotifier')
error_logger = logging.getLogger('siis.error.monitor.desktopnotifier')
signal_logger = logging.getLogger('siis.signal')


class DesktopNotifier(Notifiable):
    """
    Desktop + audible notification from some particulars signals.

    @todo seperate module and as a service + instance : one for the desktop, one for the terminal views, one for the email, one discord, to avoid blocking
    @todo config
    @todo the discord notifier part had to move to a DiscordNotifier module
    @todo add alert when trade operation is processed
    """

    AUDIO_ALERT_SIMPLE = 0
    AUDIO_ALERT_INTENSIVE = 1
    AUDIO_ALERT_WARNING = 2
    AUDIO_ALERT_HAPPY = 3

    def __init__(self):
        super().__init__("desktop")

        self.notify2 = None

        try:
            self.notify2 = import_module('notify2', package='')
        except ModuleNotFoundError as e:
            logger.error(repr(e))

        self.strategy_service = None
        self.trader_service = None
        self.watcher_service = None

        self.last_notify = 0
        self.audible = False
        self.popups = False
        self.discord = False

        self._mutex = threading.RLock()  # reentrant locker
        self._signals = collections.deque()  # filtered received signals

        self._last_stats = 0

        self._last_strategy_view = 0
        self._last_strategy_update = 0
        self._displayed_strategy = 0

        # @todo cleanup and move as conf
        self._discord_webhook = {
            # 'binance-altusdt-top.trades': 'https://discordapp.com/api/webhooks/...',
            # 'binance-altusdt-top.agg-trades': 'https://discordapp.com/api/webhooks/...',
            # 'binance-altusdt-top.closed-trades': 'https://discordapp.com/api/webhooks/...',
        }

        self._alerts = [
            ('/usr/share/sounds/info.wav', 3),
            ('/usr/share/sounds/phone.wav', 2),
            ('/usr/share/sounds/error.wav', 5),
            ('/usr/share/sounds/logout.wav', 1)
        ]

        self._audio_device = 'pulse'

        # lib notify
        if self.notify2:
            self.notify2.init('SiiS')

    def lock(self, blocking=True, timeout=-1):
        self._mutex.acquire(blocking, timeout)

    def unlock(self):
        self._mutex.release()

    def on_key_pressed(self, key):
        if Terminal.inst().mode == Terminal.MODE_DEFAULT:
            if key == 'KEY_SPREVIOUS':
                self.prev_item()
            elif key == 'KEY_SNEXT':
                self.next_item()
            elif key in ('KEY_SR', 'KEY_SF', 'KEY_SLEFT', 'KEY_SRIGHT', 'KEY_PPAGE', 'KEY_NPAGE', 'h', 'j', 'k', 'l'):
                self._last_strategy_view = 0  # force refresh

    def prev_item(self):
        self._displayed_strategy -= 1
        if self._displayed_strategy < 0:
            self._displayed_strategy = 0

        self._last_strategy_view = 0  # force refresh

    def next_item(self):
        self._displayed_strategy += 1
        self._last_strategy_view = 0  # force refresh

    @property
    def backtesting(self):
        return self.strategy_service and self.strategy_service.backtesting

    def receiver(self, signal):
        if signal.signal_type in (
                Signal.SIGNAL_SOCIAL_ENTER, Signal.SIGNAL_SOCIAL_EXIT, Signal.SIGNAL_STRATEGY_ENTRY_EXIT):

            self._signals.append(signal)

    def update(self):
        count = 0

        while self._signals:
            signal = self._signals.popleft()

            label = ""
            message = ""
            icon = "contact-new"
            now = time.time()
            audio_alert = None

            if signal.signal_type == Signal.SIGNAL_SOCIAL_ENTER:
                # here we only assume that because of what 1broker return to us but should be timestamp in the model
                entry_date = signal.data.entry_date + timedelta(hours=2)
                position_timestamp = time.mktime(entry_date.timetuple())
                audio_alert = DesktopNotifier.AUDIO_ALERT_SIMPLE

                if now - position_timestamp > 120 * 60:
                    continue

                label = "Entry position on %s" % (signal.data.symbol,)
                message = "Trader %s enter %s on %s at %s (x%s)" % (
                    signal.data.author.name if signal.data.author is not None else "???",
                    "long" if signal.data.direction == Position.LONG else "short",
                    signal.data.symbol,
                    signal.data.entry_price,
                    signal.data.leverage)

            elif signal.signal_type == Signal.SIGNAL_SOCIAL_EXIT:
                # here we only assume that because of what 1broker return to us but should be timestamp in the model
                exit_date = signal.data.exit_date + timedelta(hours=2)
                position_timestamp = time.mktime(exit_date.timetuple())
                audio_alert = DesktopNotifier.AUDIO_ALERT_SIMPLE

                if now - position_timestamp > 120 * 60:
                    continue

                label = "Exit position on %s" % (signal.data.symbol,)
                message = "Trader %s exit %s on %s at %s" % (
                    signal.data.author.name,
                    "long" if signal.data.direction == Position.LONG else "short",
                    signal.data.symbol,
                    signal.data.exit_price)

            # # @todo a threshold... or a timelimit
            # elif signal.signal_type == Signal.SIGNAL_TRADE_ALERT:
            #     icon = "go-down"
            #     label = "Position loss on %s" % (signal.data.symbol,)
            #     audio_alert = DesktopNotifier.AUDIO_ALERT_WARNING

            #     message = "Position %s %s of %s on %s start at %s %s is in regretable loss %s (%s%%) :$" % (
            #         signal.data.position_id,
            #         "long" if signal.data.direction == Position.LONG else "short",
            #         signal.data.author.name if signal.data.author is not None else "???",
            #         signal.data.trader.name,
            #         signal.data.entry_price,
            #         signal.data.symbol,
            #         signal.data.profit_loss,
            #         signal.data.profit_loss_rate * 100.0)

            # elif signal.signal_type == Signal.SIGNAL_TRADE_ENJOY:
            #     icon = "go-up"
            #     label = "Position profit on %s" % (signal.data.symbol,)
            #     audio_alert = DesktopNotifier.AUDIO_ALERT_SIMPLE

            #     message = "Position %s %s of %s on %s start at %s %s is in enjoyable profit %s (%s%%) :)" % (
            #         signal.data.position_id,
            #         "long" if signal.data.direction == Position.LONG else "short",
            #         signal.data.author.name if signal.data.author is not None else "???",
            #         signal.data.trader.name,
            #         signal.data.entry_price,
            #         signal.data.symbol,
            #         signal.data.profit_loss,
            #         signal.data.profit_loss_rate * 100.0)

            elif signal.signal_type == Signal.SIGNAL_STRATEGY_ENTRY_EXIT:
                # @todo in addition of entry/exit, modification and a reason of the exit/modification
                icon = "contact-new"
                direction = "long" if signal.data['direction'] == Position.LONG else "short"
                audio_alert = DesktopNotifier.AUDIO_ALERT_SIMPLE

                if signal.data['action'] == 'stop':
                    audio_alert = DesktopNotifier.AUDIO_ALERT_WARNING

                ldatetime = datetime.fromtimestamp(signal.data['timestamp']).strftime('%Y-%m-%d %H:%M:%S')

                label = "Signal %s %s on %s" % (signal.data['action'], direction, signal.data['symbol'],)

                message = "%s@%s (%s) %s %s at %s - #%s in %s" % (
                    signal.data['symbol'],
                    signal.data['price'],
                    signal.data['trader-name'],
                    signal.data['action'],
                    direction,
                    ldatetime,
                    signal.data['trade-id'],
                    timeframe_to_str(signal.data['timeframe']))

                if signal.data['stop-loss']:
                    message += " SL@%s" % (signal.data['stop-loss'],)

                if signal.data['take-profit']:
                    message += " TP@%s" % (signal.data['take-profit'],)

                if signal.data['profit-loss'] is not None:
                    message += " (%.2f%%)" % ((signal.data['profit-loss'] * 100),)

                if signal.data['comment'] is not None:
                    message += " (%s)" % signal.data['comment']

                if self.discord:
                    if signal.data['identifier'] in self._discord_webhook:
                        send_to_discord(self._discord_webhook[signal.data['identifier']], 'SiiS', '```' + message + '```')
                    elif self._discord_webhook.get('signals'):
                        send_to_discord(self._discord_webhook['signals'], 'SiiS', '```' + message + '```')

                # log them to the signal view
                Terminal.inst().notice(message, view="signal")

                # and in signal logger
                signal_logger.info(message)

            # process sound
            if not self.backtesting and self.audible and audio_alert:
                # if repeat
                for i in range(0, self._alerts[audio_alert[1]]):
                    subprocess.Popen(['aplay', '-D', self._audio_device, self._alerts[audio_alert[0]]], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

                # duration = 1  # second
                # freq = 440  # Hz
                # os.system('play --no-show-progress --null --channels 1 synth %s sine %f' % (duration, freq))

            if not self.backtesting and self.popups and message:
                if self.notify2:
                    n = self.notify2.Notification(label, message, icon)

                n.show()

            count += 1
            if count > 10:
                # no more than per loop
                break

        # time.sleep(0.001)  # its sync to main thread no need more

    def terminate(self):
        if self.notify2:
            self.notify2.uninit()

    def sync(self):
        # synced update
        self.update()

        if self.strategy_service:
            # discord 15m reports

            if self.discord and time.time() - self._last_stats >= 15*60:  # every 15m
                pass
            #     if self.send_discord():
            #         self._last_stats = time.time()

            # strategy stats
            if time.time() - self._last_strategy_view >= 0.5:  # every 0.5 second, refresh
                self.refresh_stats()
                self._last_strategy_view = time.time()

    #
    # discord notification @deprecated must be in a specific discordnotifier
    #

    def format_table(self, data):
        arr = tabulate(data, headers='keys', tablefmt='psql', showindex=False, floatfmt=".2f", disable_numparse=True)
        # remove colors
        arr = arr.replace(Color.ORANGE, '').replace(Color.RED, '').replace(Color.GREEN, '').replace(Color.WHITE, '').replace(Color.PURPLE, '')

        return arr

    def send_discord(self):
        for strategy in self.strategy_service.get_appliances():
            dst = None

            if strategy.identifier + '.trades' in self._discord_webhook:
                trades_dst = self._discord_webhook[strategy.identifier + '.trades']

            if strategy.identifier + '.agg-trades' in self._discord_webhook:
                agg_trades_dst = self._discord_webhook[strategy.identifier + '.agg-trades']

            if strategy.identifier + '.closed-trades' in self._discord_webhook:
                closed_trades_dst = self._discord_webhook[strategy.identifier + '.closed-trades']

            if trades_dst:
                columns, table, total_size = appl.trades_stats_table(*Terminal.inst().active_content().format(), quantities=True)
                if table:
                    arr = self.format_table(table)
                    self.split_and_send(trades_dst, arr)
            
            if agg_trades_dst:
                columns, table, total_size = appl.agg_trades_stats_table(*Terminal.inst().active_content().format(), quantities=True)
                if table:
                    arr = self.format_table(table)
                    self.split_and_send(agg_trades_dst, arr)

            if closed_trades_dst:
                columns, table, total_size = appl.closed_trades_stats_table(*Terminal.inst().active_content().format(), quantities=True)
                if table:
                    arr = self.format_table(table)
                    self.split_and_send(closed_trades_dst, arr)

        return True

    def split_and_send(self, dst, msg):
        MAX_MSG_SIZE = 2000 - 6 - 25

        rows = msg.split('\n')
        i = 0

        while i < len(rows):
            buf = ""

            while (i < len(rows)) and (len(buf) + len(rows[i]) + 1 < MAX_MSG_SIZE):
                buf += rows[i] + '\n'
                i += 1

            if buf:
                send_to_discord(dst, 'SiiS', '```' + buf + '```')

    #
    # view @deprecated must uses the new views handler
    #

    def refresh_stats(self):
        self.refresh_strategies_stats()
        self.refresh_traders_stats()

    def refresh_strategies_stats(self):
        # @todo must be in distinct View
        if not self.strategy_service:
            return

        if not (Terminal.inst().is_active('strategy') or Terminal.inst().is_active('perf') or Terminal.inst().is_active('stats')):
            return

        appliances = appl = self.strategy_service.get_appliances()
        if self._displayed_strategy >= len(appliances):
            self._displayed_strategy = 0

        appl = None

        if not appliances:
            return

        appl = self.strategy_service.get_appliances()[self._displayed_strategy]

        if not appl:
            return

        if Terminal.inst().is_active('strategy') or Terminal.inst().is_active('perf'):
            # strategy view
            if Terminal.inst().is_active('strategy'):
                num = 0

                try:                
                    columns, table, total_size = appl.trades_stats_table(*Terminal.inst().active_content().format(), quantities=True)
                    Terminal.inst().table(columns, table, total_size, view='strategy')
                    num = total_size[1]
                except Exception as e:
                    error_logger.error(repr(e))                    

                Terminal.inst().info("Active trades (%i) for strategy %s - %s" % (num, appl.name, appl.identifier), view='strategy-head')

            # perf view
            if Terminal.inst().is_active('perf'):
                num = 0

                try:
                    columns, table, total_size = appl.agg_trades_stats_table(*Terminal.inst().active_content().format(), summ=True)
                    Terminal.inst().table(columns, table, total_size, view='perf')
                    num = total_size[1]
                except Exception as e:
                    error_logger.error(repr(e))

                Terminal.inst().info("Perf per market trades (%i) for strategy %s - %s" % (num, appl.name, appl.identifier), view='perf-head')

        # stats view
        if Terminal.inst().is_active('stats'):
            num = 0

            try:
                columns, table, total_size = appl.closed_trades_stats_table(*Terminal.inst().active_content().format(), quantities=True)
                Terminal.inst().table(columns, table, total_size, view='stats')
                num = total_size[1]
            except Exception as e:
                    error_logger.error(repr(e))

            Terminal.inst().info("Trade history (%i) for strategy %s - %s" % (num, appl.name, appl.identifier), view='stats-head')

    def refresh_traders_stats(self):
        if not self.trader_service:
            return

        # account view
        if Terminal.inst().is_active('account'):
            traders = self.trader_service.get_traders()

            if len(traders) > 0:
                trader = next(iter(traders))
                num = 0

                try:
                    columns, table, total_size = trader.account_table(*Terminal.inst().active_content().format())
                    Terminal.inst().table(columns, table, total_size, view='account')
                    num = total_size[1]
                except:
                    pass

                Terminal.inst().info("Account details (%i) for trader %s - %s" % (num, trader.name, trader.account.name), view='account-head')

        # tickers view
        if Terminal.inst().is_active('ticker'):
            traders = self.trader_service.get_traders()

            if len(traders) > 0:
                trader = next(iter(traders))
                num = 0

                try:
                    columns, table, total_size = trader.markets_tickers_table(*Terminal.inst().active_content().format(), prev_timestamp=self._last_strategy_update)
                    Terminal.inst().table(columns, table, total_size, view='ticker')
                    num = total_size[1]
                except:
                    pass

                Terminal.inst().info("Tickers list (%i) for tader %s on account %s" % (num, trader.name, trader.account.name), view='ticker-head')

        # markets view
        if Terminal.inst().is_active('market'):
            traders = self.trader_service.get_traders()

            if len(traders) > 0:
                trader = next(iter(traders))
                num = 0

                try:
                    columns, table, total_size = trader.markets_table(*Terminal.inst().active_content().format())
                    Terminal.inst().table(columns, table, total_size, view='market')
                    num = total_size[1]
                except Exception as e:
                    error_logger.error(repr(e))

                Terminal.inst().info("Market list (%i) trader %s on account %s" % (num, trader.name, trader.account.name), view='market-head')

        # assets view
        if Terminal.inst().is_active('asset'):
            traders = self.trader_service.get_traders()

            if len(traders) > 0:
                trader = next(iter(traders))
                num = 0

                try:
                    columns, table, total_size = trader.assets_table(*Terminal.inst().active_content().format())
                    Terminal.inst().table(columns, table, total_size, view='asset')
                    num = total_size[1]
                except Exception as e:
                    error_logger.error(repr(e))

                Terminal.inst().info("Asset list (%i) trader %s on account %s" % (num, trader.name, trader.account.name), view='asset-head')

        self._last_strategy_update = self.strategy_service.timestamp
