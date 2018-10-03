import logging
import sched
import threading
import time
from copy import deepcopy
from datetime import datetime
from ssl import SSLError
from urllib.error import URLError

import wx
import wx.dataview
from wx.lib.newevent import NewCommandEvent, NewEvent

from nuxhash import nicehash, utils
from nuxhash.devices.nvidia import NvidiaDevice
from nuxhash.gui import main
from nuxhash.miners.excavator import Excavator
from nuxhash.nicehash import unpaid_balance
from nuxhash.settings import DEFAULT_SETTINGS, EMPTY_BENCHMARKS
from nuxhash.switching.naive import NaiveSwitcher


MINING_UPDATE_SECS = 5
BALANCE_UPDATE_MIN = 5
NVIDIA_COLOR = (66, 244, 69)

StartMiningEvent, EVT_START_MINING = NewCommandEvent()
StopMiningEvent, EVT_STOP_MINING = NewCommandEvent()
MiningStatusEvent, EVT_MINING_STATUS = NewEvent()
NewBalanceEvent, EVT_BALANCE = NewEvent()


class MiningScreen(wx.Panel):

    def __init__(self, parent, *args, devices=[], frame=None, **kwargs):
        wx.Panel.__init__(self, parent, *args, **kwargs)
        self._mining = False
        self._thread = None
        self._frame = frame
        self._devices = devices
        self.Bind(main.EVT_SETTINGS, self.OnNewSettings)
        self.Bind(main.EVT_BENCHMARKS, self.OnNewBenchmarks)
        self.Bind(EVT_BALANCE, self.OnNewBalance)
        self.Bind(EVT_MINING_STATUS, self.OnMiningStatus)

        sizer = wx.BoxSizer(orient=wx.VERTICAL)
        self.SetSizer(sizer)

        # Update balance periodically.
        self.Bind(EVT_BALANCE, self.OnNewBalance)
        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, lambda event: self._update_balance(), self._timer)
        self._timer.Start(milliseconds=BALANCE_UPDATE_MIN*60*1e3)

        # Add mining panel.
        self._panel = MiningPanel(self, frame=frame)
        sizer.Add(self._panel, wx.SizerFlags().Border(wx.LEFT|wx.RIGHT|wx.TOP,
                                                      main.PADDING_PX)
                                              .Proportion(1.0)
                                              .Expand())

        bottom_sizer = wx.BoxSizer(orient=wx.HORIZONTAL)
        sizer.Add(bottom_sizer, wx.SizerFlags().Border(wx.ALL, main.PADDING_PX)
                                               .Expand())

        # Add balance displays.
        balances = wx.FlexGridSizer(2, 2, main.PADDING_PX)
        balances.AddGrowableCol(1)
        bottom_sizer.Add(balances, wx.SizerFlags().Proportion(1.0).Expand())

        balances.Add(wx.StaticText(self, label='Daily revenue'))
        self._revenue = wx.StaticText(self,
                                      style=wx.ALIGN_RIGHT|wx.ST_NO_AUTORESIZE)
        self._revenue.SetFont(self.GetFont().Bold())
        balances.Add(self._revenue, wx.SizerFlags().Expand())

        balances.Add(wx.StaticText(self, label='Address balance'))
        self._balance = wx.StaticText(self,
                                      style=wx.ALIGN_RIGHT|wx.ST_NO_AUTORESIZE)
        self._balance.SetFont(self.GetFont().Bold())
        balances.Add(self._balance, wx.SizerFlags().Expand())

        bottom_sizer.AddSpacer(main.PADDING_PX)

        # Add start/stop button.
        self._startstop = wx.Button(self, label='Start Mining')
        bottom_sizer.Add(self._startstop, wx.SizerFlags().Expand()
                                                         .Center())
        self.Bind(wx.EVT_BUTTON, self.OnStartStop, self._startstop)

        self._update_balance()

    def OnNewSettings(self, event):
        self._update_balance()

    def OnNewBenchmarks(self, event):
        pass

    def _update_balance(self):
        address = self._frame.settings['nicehash']['wallet']
        def request(address, target):
            balance = unpaid_balance(address)
            wx.PostEvent(target, NewBalanceEvent(balance=balance))
        thread = threading.Thread(target=request, args=(address, self))
        thread.start()

    def OnStartStop(self, event):
        if self._mining:
            wx.PostEvent(self._panel, StopMiningEvent(id=wx.ID_ANY))

            self._revenue.SetLabel('')
            self._startstop.SetLabel('Start Mining')

            self._stop_thread()
            self._mining = False
        else:
            wx.PostEvent(self._panel, StartMiningEvent(id=wx.ID_ANY))

            self._startstop.SetLabel('Stop Mining')

            self._start_thread()
            self._mining = True

    def _start_thread(self):
        if not self._thread:
            self._thread = MiningThread(
                devices=self._devices, window=self,
                settings=deepcopy(self._frame.settings),
                benchmarks=deepcopy(self._frame.benchmarks))
            self._thread.start()

    def _stop_thread(self):
        if self._thread:
            self._thread.stop()
            self._thread.join()
            self._thread = None

    def OnNewBalance(self, event):
        unit = self._frame.settings['gui']['units']
        self._balance.SetLabel(utils.format_balance(event.balance, unit))

    def OnMiningStatus(self, event):
        total_revenue = sum(event.revenue.values())
        unit = self._frame.settings['gui']['units']
        self._revenue.SetLabel(utils.format_balance(total_revenue, unit))
        wx.PostEvent(self._panel, event)


class MiningPanel(wx.dataview.DataViewListCtrl):

    def __init__(self, parent, *args, frame=None, **kwargs):
        wx.dataview.DataViewListCtrl.__init__(self, parent, *args, **kwargs)
        self._frame = frame
        self.Bind(EVT_START_MINING, self.OnStartMining)
        self.Bind(EVT_STOP_MINING, self.OnStopMining)
        self.Bind(EVT_MINING_STATUS, self.OnMiningStatus)
        self.Disable()
        self.AppendTextColumn('Algorithm', width=wx.COL_WIDTH_AUTOSIZE)
        self.AppendColumn(
            wx.dataview.DataViewColumn('Devices', DeviceListRenderer(),
                                       1, align=wx.ALIGN_LEFT,
                                       width=wx.COL_WIDTH_AUTOSIZE),
            'string')
        self.AppendTextColumn('Speed', width=wx.COL_WIDTH_AUTOSIZE)
        self.AppendTextColumn('Revenue')

    def OnStartMining(self, event):
        self.Enable()

    def OnStopMining(self, event):
        self.Disable()
        self.DeleteAllItems()

    def OnMiningStatus(self, event):
        self.DeleteAllItems()
        algorithms = list(event.speeds.keys())
        algorithms.sort(key=lambda algorithm: algorithm.name)
        for algorithm in algorithms:
            algo = '%s\n(%s)' % (algorithm.name, ', '.join(algorithm.algorithms))
            devices = ','.join([MiningPanel._device_to_string(device)
                                for device in event.devices[algorithm]])
            speed = ',\n'.join([utils.format_speed(speed)
                                for speed in event.speeds[algorithm]])
            revenue = utils.format_balance(event.revenue[algorithm],
                                           self._frame.settings['gui']['units'])
            self.AppendItem([algo, devices, speed, revenue])

    def _device_to_string(device):
        if isinstance(device, NvidiaDevice):
            name = device.name
            name = name.replace('GeForce', '')
            name = name.replace('GTX', '')
            name = name.replace('RTX', '')
            name = name.strip()
            return 'N:%s' % name
        else:
            raise Exception('bad device instance')


class DeviceListRenderer(wx.dataview.DataViewCustomRenderer):

    CORNER_RADIUS = 5

    def __init__(self, *args, **kwargs):
        wx.dataview.DataViewCustomRenderer.__init__(self, *args, **kwargs)
        self.devices = []
        self._colordb = wx.ColourDatabase()

    def SetValue(self, value):
        vendors = {
            'N': 'nvidia'
            }
        self.devices = [{ 'name': s[2:], 'vendor': vendors[s[0]] }
                        for s in value.split(',')]
        return True

    def GetValue(self):
        tags = {
            'nvidia': 'N'
            }
        return ','.join(['%s:%s' % (tags[device['vendor']], device['name'])
                         for device in self.devices])

    def GetSize(self):
        boxes = [self.GetTextExtent(device['name']) for device in self.devices]
        return wx.Size((max(box.GetWidth() for box in boxes)
                        + DeviceListRenderer.CORNER_RADIUS*2),
                       (sum(box.GetHeight() for box in boxes)
                        + DeviceListRenderer.CORNER_RADIUS*2*len(boxes)
                        + DeviceListRenderer.CORNER_RADIUS*(len(boxes) - 1)))

    def Render(self, cell, dc, state):
        position = cell.GetPosition()
        for device in self.devices:
            box = self.GetTextExtent(device['name'])

            if device['vendor'] == 'nvidia':
                color = self._colordb.Find('LIME GREEN')
            else:
                color = self._colordb.Find('LIGHT GREY')
            dc.SetBrush(wx.Brush(color))
            dc.SetPen(wx.TRANSPARENT_PEN)
            shade_rect = wx.Rect(
                position,
                wx.Size(box.GetWidth() + DeviceListRenderer.CORNER_RADIUS*2,
                        box.GetHeight() + DeviceListRenderer.CORNER_RADIUS*2))
            dc.DrawRoundedRectangle(shade_rect, DeviceListRenderer.CORNER_RADIUS)

            text_rect = wx.Rect(
                wx.Point(position.x + DeviceListRenderer.CORNER_RADIUS,
                         position.y + DeviceListRenderer.CORNER_RADIUS),
                box)
            self.RenderText(device['name'], 0, text_rect, dc, state)

            position = wx.Point(position.x,
                                (position.y + box.GetHeight()
                                 + DeviceListRenderer.CORNER_RADIUS*2
                                 + DeviceListRenderer.CORNER_RADIUS))
        return True


class MiningThread(threading.Thread):

    PROFIT_PRIORITY = 1
    STATUS_PRIORITY = 2
    STOP_PRIORITY = 0

    def __init__(self, devices=[], window=None,
                 settings=DEFAULT_SETTINGS, benchmarks=EMPTY_BENCHMARKS):
        threading.Thread.__init__(self)
        self._window = window
        self._settings = settings
        self._benchmarks = benchmarks
        self._devices = devices
        self._stop_signal = threading.Event()
        self._scheduler = sched.scheduler(
            time.time, lambda t: self._stop_signal.wait(t))

    def run(self):
        # Initialize miners.
        stratums = None
        while stratums is None:
            try:
                payrates, stratums = nicehash.simplemultialgo_info(
                    self._settings)
            except (socket.error, socket.timeout, SSLError, URLError):
                time.sleep(5)
        self._miners = [Excavator(main.CONFIG_DIR, self._settings)]
        for miner in self._miners:
            miner.stratums = stratums
        self._algorithms = sum([miner.algorithms for miner in self._miners], [])

        # Initialize profit-switching.
        self._profit_switch = NaiveSwitcher(self._settings)
        self._profit_switch.reset()

        self._scheduler.enter(0, MiningThread.PROFIT_PRIORITY, self._switch_algos)
        self._scheduler.enter(0, MiningThread.STATUS_PRIORITY, self._read_status)
        self._scheduler.run()

    def stop(self):
        self._scheduler.enter(0, MiningThread.STOP_PRIORITY, self._stop_mining)
        self._stop_signal.set()

    def _switch_algos(self):
        # Get profitability information from NiceHash.
        try:
            payrates, stratums = nicehash.simplemultialgo_info(
                self._settings)
        except (socket.error, socket.timeout, SSLError, URLError) as err:
            logging.warning('NiceHash stats: %s' % err)
        except nicehash.BadResponseError:
            logging.warning('NiceHash stats: Bad response')
        else:
            download_time = datetime.now()
            self._current_payrates = payrates

        # Calculate BTC/day rates.
        def revenue(device, algorithm):
            benchmarks = self._benchmarks[device]
            if algorithm.name in benchmarks:
                return sum([payrates[algorithm.algorithms[i]]
                            *benchmarks[algorithm.name][i]
                            for i in range(len(algorithm.algorithms))])
            else:
                return 0.0
        revenues = {device: {algorithm: revenue(device, algorithm)
                             for algorithm in self._algorithms}
                    for device in self._devices}

        # Get device -> algorithm assignments from profit switcher.
        assigned_algorithm = self._profit_switch.decide(revenues, download_time)
        self._assignments = assigned_algorithm
        for this_algorithm in self._algorithms:
            this_devices = [device for device, algorithm
                            in assigned_algorithm.items()
                            if algorithm == this_algorithm]
            this_algorithm.set_devices(this_devices)

        self._scheduler.enter(self._settings['switching']['interval'],
                              MiningThread.PROFIT_PRIORITY, self._switch_algos)

    def _read_status(self):
        running_algorithms = self._assignments.values()
        # Check miner status.
        for algorithm in running_algorithms:
            if not algorithm.parent.is_running():
                logging.error('Detected %s crash, restarting miner'
                              % algorithm.name)
                algorithm.parent.reload()
        speeds = {algorithm: algorithm.current_speeds()
                  for algorithm in running_algorithms}
        revenue = {algorithm: sum([self._current_payrates[multialgorithm]
                                   *speeds[algorithm][i]
                                   for i, multialgorithm
                                   in enumerate(algorithm.algorithms)])
                    for algorithm in running_algorithms}
        devices = {algorithm: [device for device, this_algorithm
                               in self._assignments.items()
                               if this_algorithm == algorithm]
                   for algorithm in running_algorithms}
        wx.PostEvent(self._window,
                     MiningStatusEvent(speeds=speeds, revenue=revenue,
                                       devices=devices))
        self._scheduler.enter(MINING_UPDATE_SECS, MiningThread.STATUS_PRIORITY,
                              self._read_status)

    def _stop_mining(self):
        logging.info('Stopping mining')
        for algorithm in self._algorithms:
            algorithm.set_devices([])
        for miner in self._miners:
            miner.unload()
        # Empty the scheduler.
        for job in self._scheduler.queue:
            self._scheduler.cancel(job)

