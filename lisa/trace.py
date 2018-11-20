# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2015, ARM Limited and contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

""" Trace Parser Module """

import numpy as np
import os
import os.path
import pandas as pd
import sys
import trappy
import json
import warnings
import operator
import logging
import webbrowser
from functools import reduce

from lisa.analysis.proxy import AnalysisProxy
from lisa.utils import Loggable, memoized
from lisa.platforms.platinfo import PlatformInfo
from devlib.target import KernelVersion
from trappy.utils import listify, handle_duplicate_index


NON_IDLE_STATE = -1

class Trace(Loggable):
    """
    The Trace object is the LISA trace events parser.

    :param data_dir: folder containing all trace data
    :type data_dir: str

    :param events: events to be parsed (all the events by default)
    :type events: str or list(str)

    :param platform: a dictionary containing information about the target
        platform
    :type platform: dict

    :param window: time window to consider when parsing the trace
    :type window: tuple(int, int)

    :param normalize_time: normalize trace time stamps
    :type normalize_time: bool

    :param trace_format: format of the trace. Possible values are:
        - FTrace
        - SysTrace
    :type trace_format: str

    :param plots_dir: directory where to save plots
    :type plots_dir: str

    :param plots_prefix: prefix for plots file names
    :type plots_prefix: str
    """

    def __init__(self,
                 data_dir,
                 plat_info=None,
                 events=None,
                 window=(0, None),
                 normalize_time=True,
                 trace_format='FTrace',
                 plots_dir=None,
                 plots_prefix=''):
        logger = self.get_logger()

        if plat_info is None:
            plat_info = PlatformInfo()

        # The platform information used to run the experiments
        self.plat_info = plat_info

        # TRAPpy Trace object
        self.ftrace = None

        # Trace format
        self.trace_format = trace_format

        # The time window used to limit trace parsing to
        self.window = window

        # Whether trace timestamps are normalized or not
        self.normalize_time = normalize_time

        # Dynamically registered TRAPpy events
        self.trappy_cls = {}

        # Maximum timespan for all collected events
        self.time_range = 0

        # Time the system was overutilzied
        self.overutilized_time = 0
        self.overutilized_prc = 0

        # List of events required by user
        self.events = []

        # List of events available in the parsed trace
        self.available_events = []

        # Cluster frequency coherency flag
        self.freq_coherency = True

        # Folder containing trace
        self.data_dir = data_dir

        # By deafult, use the trace dir to save plots
        self.plots_dir = plots_dir
        if self.plots_dir is None:
            self.plots_dir = self.data_dir
        self.plots_prefix = plots_prefix

        self.__registerTraceEvents(events)
        self.__parseTrace(self.data_dir, window, trace_format)

        self.analysis = AnalysisProxy(self)

    @property
    @memoized
    def cpus_count(self):
        try:
            return self.plat_info['cpus-count']
        # If we don't know the number of CPUs, check the trace for the
        # highest-numbered CPU that traced an event.
        except KeyError:
            max_cpu = max(int(self.df_events(e)['__cpu'].max())
                          for e in self.available_events)
            return max_cpu + 1

    def setXTimeRange(self, t_min=None, t_max=None):
        """
        Set x axis time range to the specified values.

        :param t_min: lower bound
        :type t_min: int or float

        :param t_max: upper bound
        :type t_max: int or float
        """
        self.x_min = t_min if t_min is not None else self.start_time
        self.x_max = t_max if t_max is not None else self.start_time + self.time_range

        self.get_logger().debug('Set plots time range to (%.6f, %.6f)[s]',
                       self.x_min, self.x_max)

    def __registerTraceEvents(self, events):
        """
        Save a copy of the parsed events.

        :param events: single event name or list of events names
        :type events: str or list(str)
        """
        # Parse all events by default
        if events is None:
            self.events = []
            return
        if isinstance(events, str):
            self.events = events.split(' ')
        elif isinstance(events, list):
            self.events = events
        else:
            raise ValueError('Events must be a string or a list of strings')
        # Register devlib fake cpu_frequency events
        if 'cpu_frequency' in events:
            self.events.append('cpu_frequency_devlib')

    def __parseTrace(self, path, window, trace_format):
        """
        Internal method in charge of performing the actual parsing of the
        trace.

        :param path: path to the trace folder (or trace file)
        :type path: str

        :param window: time window to consider when parsing the trace
        :type window: tuple(int, int)

        :param trace_format: format of the trace. Possible values are:
            - FTrace
            - SysTrace
        :type trace_format: str
        """
        logger = self.get_logger()
        logger.debug('Loading [sched] events from trace in [%s]...', path)
        logger.debug('Parsing events: %s', self.events)
        if trace_format.upper() == 'SYSTRACE' or path.endswith('html'):
            logger.debug('Parsing SysTrace format...')
            trace_class = trappy.SysTrace
            self.trace_format = 'SysTrace'
        elif trace_format.upper() == 'FTRACE':
            logger.debug('Parsing FTrace format...')
            trace_class = trappy.FTrace
            self.trace_format = 'FTrace'
        else:
            raise ValueError("Unknown trace format {}".format(trace_format))

        # If using normalized time, we should use
        # TRAPpy's `abs_window` instead of `window`
        window_kw = {}
        if self.normalize_time:
            window_kw['window'] = window
        else:
            window_kw['abs_window'] = window

        # Make sure event names are not unicode strings
        self.ftrace = trace_class(path, scope="custom", events=self.events,
                                  normalize_time=self.normalize_time, **window_kw)

        # Load Functions profiling data
        has_function_stats = self._loadFunctionsStats(path)

        # Check for events available on the parsed trace
        self.__checkAvailableEvents()
        if len(self.available_events) == 0:
            if has_function_stats:
                logger.info('Trace contains only functions stats')
                return
            raise ValueError('The trace does not contain useful events '
                             'nor function stats')

        # Index PIDs and Task names
        self.__loadTasksNames()

        self.__computeTimeSpan()

        # Setup internal data reference to interesting events/dataframes
        self._sanitize_SchedLoadAvgCpu()
        self._sanitize_SchedLoadAvgTask()
        self._sanitize_SchedCpuCapacity()
        self._sanitize_SchedBoostCpu()
        self._sanitize_SchedBoostTask()
        self._sanitize_SchedEnergyDiff()
        self._sanitize_SchedOverutilized()
        self._sanitize_CpuFrequency()
        self._sanitize_ThermalPowerCpu()

    def __checkAvailableEvents(self, key=""):
        """
        Internal method used to build a list of available events.

        :param key: key to be used for TRAPpy filtering
        :type key: str
        """
        logger = self.get_logger()
        for val in self.ftrace.get_filters(key):
            obj = getattr(self.ftrace, val)
            if len(obj.data_frame):
                self.available_events.append(val)
        logger.debug('Events found on trace:')
        for evt in self.available_events:
            logger.debug(' - %s', evt)

    def __loadTasksNames(self):
        """
        Try to load tasks names using one of the supported events.
        """
        def load(event, name_key, pid_key):
            df = self.df_events(event)
            self._scanTasks(df, name_key=name_key, pid_key=pid_key)

        if 'sched_switch' in self.available_events:
            load('sched_switch', 'prev_comm', 'prev_pid')
            return

        if 'sched_load_avg_task' in self.available_events:
            load('sched_load_avg_task', 'comm', 'pid')
            return

        self.get_logger().warning('Failed to load tasks names from trace events')

    def hasEvents(self, dataset):
        """
        Returns True if the specified event is present in the parsed trace,
        False otherwise.

        :param dataset: trace event name or list of trace events
        :type dataset: str or list(str)
        """
        if isinstance(dataset, str):
            return dataset in self.available_events

        return set(dataset).issubset(set(self.available_events))

    def __computeTimeSpan(self):
        """
        Compute time axis range, considering all the parsed events.
        """
        self.start_time = 0 if self.normalize_time else self.ftrace.basetime
        self.time_range = self.ftrace.get_duration()
        self.get_logger().debug('Collected events spans a %.3f [s] time interval',
                       self.time_range)

        self.setXTimeRange(max(self.start_time, self.window[0]), self.window[1])

    def _scanTasks(self, df, name_key='comm', pid_key='pid'):
        """
        Extract tasks names and PIDs from the input data frame. The data frame
        should contain a task name column and PID column.

        :param df: data frame containing trace events from which tasks names
            and PIDs will be extracted
        :type df: :mod:`pandas.DataFrame`

        :param name_key: The name of the dataframe columns containing task
            names
        :type name_key: str

        :param pid_key: The name of the dataframe columns containing task PIDs
        :type pid_key: str
        """
        df = df[[name_key, pid_key]]
        self._tasks_by_pid = (df.drop_duplicates(subset=pid_key, keep='last')
                .rename(columns={
                    pid_key : 'PID',
                    name_key : 'TaskName'})
                .set_index('PID').sort_index())

    def get_task_by_name(self, name):
        """
        Get the PIDs of all tasks with the specified name.

        The same PID can have different task names, mainly because once a task
        is generated it inherits the parent name and then its name is updated
        to represent what the task really is.

        This API works under the assumption that a task name is updated at
        most one time and it always considers the name a task had the last time
        it has been scheduled for execution in the current trace.

        :param name: task name
        :type name: str

        :return: a list of PID for tasks which name matches the required one,
                 the last time they ran in the current trace
        """
        return (self._tasks_by_pid[self._tasks_by_pid.TaskName == name]
                    .index.tolist())

    def get_task_by_pid(self, pid):
        """
        Get the name of the task with the specified PID.

        The same PID can have different task names, mainly because once a task
        is generated it inherits the parent name and then its name is
        updated to represent what the task really is.

        This API works under the assumption that a task name is updated at
        most one time and it always report the name the task had the last time
        it has been scheduled for execution in the current trace.

        :param name: task PID
        :type name: int

        :return: the name of the task which PID matches the required one,
                 the last time they ran in the current trace
        """
        try:
            return self._tasks_by_pid.ix[pid].values[0]
        except KeyError:
            return None

    def get_task_pid(self, task):
        """
        Helper that takes either a name or a PID and always returns a PID

        :param task: Either the task name or the task PID
        :type task: int or str
        """
        if isinstance(task, str):
            pid_list = self.get_task_by_name(task)
            if len(pid_list) > 1:
                self.get_logger().warning(
                    "More than one PID found for task {}, "
                    "using the first one ({})".format(task, pid_list[0]))
            pid = pid_list[0]
        else:
            pid = task

        return pid


    def get_tasks(self):
        """
        Get a dictionary of all the tasks in the Trace.

        :return: a dictionary which maps each PID to the corresponding task
                 name
        """
        return self._tasks_by_pid.TaskName.to_dict()

    def show(self):
        """
        Open the parsed trace using the most appropriate native viewer.

        The native viewer depends on the specified trace format:
        - ftrace: open using kernelshark
        - systrace: open using a browser

        In both cases the native viewer is assumed to be available in the host
        machine.
        """
        if isinstance(self.ftrace, trappy.FTrace):
            return os.popen("kernelshark '{}'".format(self.ftrace.trace_path))
        if isinstance(self.ftrace, trappy.SysTrace):
            return webbrowser.open(self.ftrace.trace_path)
        self.get_logger().warning('No trace data available')


###############################################################################
# DataFrame Getter Methods
###############################################################################

    def df(self, event):
        """
        Get a dataframe containing all occurrences of the specified trace event
        in the parsed trace.

        :param event: Trace event name
        :type event: str
        """
        warnings.simplefilter('always', DeprecationWarning) #turn off filter
        warnings.warn("\n\tUse of Trace::df() is deprecated and will be soon removed."
                      "\n\tUse Trace::df_events(event_name) instead.",
                      category=DeprecationWarning)
        warnings.simplefilter('default', DeprecationWarning) #reset filter
        return self.df_events(event)

    def df_events(self, event):
        """
        Get a dataframe containing all occurrences of the specified trace event
        in the parsed trace.

        :param event: Trace event name
        :type event: str
        """
        if self.data_dir is None:
            raise ValueError("trace data not (yet) loaded")
        if self.ftrace and hasattr(self.ftrace, event):
            return getattr(self.ftrace, event).data_frame
        raise ValueError('Event [{}] not supported. '
                         'Supported events are: {}'
                         .format(event, self.available_events))

    def df_functions_stats(self, functions=None):
        """
        Get a DataFrame of specified kernel functions profile data

        For each profiled function a DataFrame is returned which reports stats
        on kernel functions execution time. The reported stats are per-CPU and
        includes: number of times the function has been executed (hits),
        average execution time (avg), overall execution time (time) and samples
        variance (s_2).
        By default returns a DataFrame of all the functions profiled.

        :param functions: the name of the function or a list of function names
                          to report
        :type functions: str or list(str)
        """
        if not hasattr(self, '_functions_stats_df'):
            return None
        df = self._functions_stats_df
        if not functions:
            return df
        return df.loc[df.index.get_level_values(1).isin(listify(functions))]


###############################################################################
# Trace Events Sanitize Methods
###############################################################################
    def _sanitize_SchedCpuCapacity(self):
        """
        Add more columns to cpu_capacity data frame if the energy model is
        available and the platform is big.LITTLE.
        """
        if not self.hasEvents('cpu_capacity') \
           or 'nrg-model' not in self.plat_info \
           or not self.has_big_little:
            return

        df = self.df_events('cpu_capacity')

        # Add column with LITTLE and big CPUs max capacities
        nrg_model = self.plat_info['nrg-model']
        max_lcap = nrg_model['little']['cpu']['cap_max']
        max_bcap = nrg_model['big']['cpu']['cap_max']
        df['max_capacity'] = np.select(
                [df.cpu.isin(self.plat_info['clusters']['little'])],
                [max_lcap], max_bcap)
        # Add LITTLE and big CPUs "tipping point" threshold
        tip_lcap = 0.8 * max_lcap
        tip_bcap = 0.8 * max_bcap
        df['tip_capacity'] = np.select(
                [df.cpu.isin(self.plat_info['clusters']['little'])],
                [tip_lcap], tip_bcap)

    def _sanitize_SchedLoadAvgCpu(self):
        """
        If necessary, rename certain signal names from v5.0 to v5.1 format.
        """
        if not self.hasEvents('sched_load_avg_cpu'):
            return
        df = self.df_events('sched_load_avg_cpu')
        if 'utilization' in df:
            df.rename(columns={'utilization': 'util_avg'}, inplace=True)
            df.rename(columns={'load': 'load_avg'}, inplace=True)

    def _sanitize_SchedLoadAvgTask(self):
        """
        If necessary, rename certain signal names from v5.0 to v5.1 format.
        """
        if not self.hasEvents('sched_load_avg_task'):
            return
        df = self.df_events('sched_load_avg_task')
        if 'utilization' in df:
            df.rename(columns={'utilization': 'util_avg'}, inplace=True)
            df.rename(columns={'load': 'load_avg'}, inplace=True)
            df.rename(columns={'avg_period': 'period_contrib'}, inplace=True)
            df.rename(columns={'runnable_avg_sum': 'load_sum'}, inplace=True)
            df.rename(columns={'running_avg_sum': 'util_sum'}, inplace=True)

    def _sanitize_SchedBoostCpu(self):
        """
        Add a boosted utilization signal as the sum of utilization and margin.

        Also, if necessary, rename certain signal names from v5.0 to v5.1
        format.
        """
        if not self.hasEvents('sched_boost_cpu'):
            return
        df = self.df_events('sched_boost_cpu')
        if 'usage' in df:
            df.rename(columns={'usage': 'util'}, inplace=True)
        df['boosted_util'] = df['util'] + df['margin']

    def _sanitize_SchedBoostTask(self):
        """
        Add a boosted utilization signal as the sum of utilization and margin.

        Also, if necessary, rename certain signal names from v5.0 to v5.1
        format.
        """
        if not self.hasEvents('sched_boost_task'):
            return
        df = self.df_events('sched_boost_task')
        if 'utilization' in df:
            # Convert signals name from to v5.1 format
            df.rename(columns={'utilization': 'util'}, inplace=True)
        df['boosted_util'] = df['util'] + df['margin']

    def _sanitize_SchedEnergyDiff(self):
        """
        If a energy model is provided, some signals are added to the
        sched_energy_diff trace event data frame.

        Also convert between existing field name formats for sched_energy_diff
        """
        logger = self.get_logger()
        if not self.hasEvents('sched_energy_diff') \
           or 'nrg-model' not in self.plat_info \
           or not self.has_big_little:
            return
        nrg_model = self.plat_info['nrg-model']
        em_lcluster = nrg_model['little']['cluster']
        em_bcluster = nrg_model['big']['cluster']
        em_lcpu = nrg_model['little']['cpu']
        em_bcpu = nrg_model['big']['cpu']
        lcpus = len(self.plat_info['clusters']['little'])
        bcpus = len(self.plat_info['clusters']['big'])
        SCHED_LOAD_SCALE = 1024

        power_max = em_lcpu['nrg_max'] * lcpus + em_bcpu['nrg_max'] * bcpus + \
            em_lcluster['nrg_max'] + em_bcluster['nrg_max']
        logger.debug(
            "Maximum estimated system energy: {0:d}".format(power_max))

        df = self.df_events('sched_energy_diff')

        translations = {'nrg_d' : 'nrg_diff',
                        'utl_d' : 'usage_delta',
                        'payoff' : 'nrg_payoff'
        }
        df.rename(columns=translations, inplace=True)

        df['nrg_diff_pct'] = SCHED_LOAD_SCALE * df.nrg_diff / power_max

        # Tag columns by usage_delta
        ccol = df.usage_delta
        df['usage_delta_group'] = np.select(
            [ccol < 150, ccol < 400, ccol < 600],
            ['< 150', '< 400', '< 600'], '>= 600')

        # Tag columns by nrg_payoff
        ccol = df.nrg_payoff
        df['nrg_payoff_group'] = np.select(
            [ccol > 2e9, ccol > 0, ccol > -2e9],
            ['Optimal Accept', 'SchedTune Accept', 'SchedTune Reject'],
            'Suboptimal Reject')

    def _sanitize_SchedOverutilized(self):
        """ Add a column with overutilized status duration. """
        if not self.hasEvents('sched_overutilized'):
            return

        df = self.df_events('sched_overutilized')
        self.add_events_deltas(df, 'len')

        # Build a stat on trace overutilization
        self.overutilized_time = df[df.overutilized == 1].len.sum()
        self.overutilized_prc = 100. * self.overutilized_time / self.time_range

        self.get_logger().debug('Overutilized time: %.6f [s] (%.3f%% of trace time)',
                        self.overutilized_time, self.overutilized_prc)

    def _sanitize_ThermalPowerCpu(self):
        self._sanitize_ThermalPowerCpuGetPower()
        self._sanitize_ThermalPowerCpuLimit()

    def _sanitize_ThermalPowerCpuMask(self, mask):
        # Replace '00000000,0000000f' format in more usable int
        return int(mask.replace(',', ''), 16)

    def _sanitize_ThermalPowerCpuGetPower(self):
        if not self.hasEvents('thermal_power_cpu_get_power'):
            return

        df = self.df_events('thermal_power_cpu_get_power')

        df['cpus'] = df['cpus'].apply(
            self._sanitize_ThermalPowerCpuMask
        )

    def _sanitize_ThermalPowerCpuLimit(self):
        if not self.hasEvents('thermal_power_cpu_limit'):
            return

        df = self.df_events('thermal_power_cpu_limit')

        df['cpus'] = df['cpus'].apply(
            self._sanitize_ThermalPowerCpuMask
        )

    def _chunker(self, seq, size):
        """
        Given a data frame or a series, generate a sequence of chunks of the
        given size.

        :param seq: data to be split into chunks
        :type seq: :class:`pandas.Series` or :class:`pandas.DataFrame`

        :param size: size of each chunk
        :type size: int
        """
        return (seq.iloc[pos:pos + size] for pos in range(0, len(seq), size))

    def _sanitize_CpuFrequency(self):
        """
        Verify that all platform reported clusters are frequency coherent (i.e.
        frequency scaling is performed at a cluster level).
        """
        logger = self.get_logger()
        if not self.hasEvents('cpu_frequency_devlib') \
           or 'freq-domains' not in self.plat_info:
            return

        devlib_freq = self.df_events('cpu_frequency_devlib')
        devlib_freq.rename(columns={'cpu_id':'cpu'}, inplace=True)
        devlib_freq.rename(columns={'state':'frequency'}, inplace=True)

        df = self.df_events('cpu_frequency')
        domains = self.plat_info['freq-domains']

        # devlib always introduces fake cpu_frequency events, in case the
        # OS has not generated cpu_frequency envets there are the only
        # frequency events to report
        if len(df) == 0:
            # Register devlib injected events as 'cpu_frequency' events
            setattr(self.ftrace.cpu_frequency, 'data_frame', devlib_freq)
            df = devlib_freq
            self.available_events.append('cpu_frequency')

        # make sure fake cpu_frequency events are never interleaved with
        # OS generated events
        else:
            if len(devlib_freq) > 0:

                # Frequencies injection is done in a per-cluster based.
                # This is based on the assumption that clusters are
                # frequency choerent.
                # For each cluster we inject devlib events only if
                # these events does not overlaps with os-generated ones.

                # Inject "initial" devlib frequencies
                os_df = df
                dl_df = devlib_freq.iloc[:self.cpus_count]
                for cpus in domains:
                    dl_freqs = dl_df[dl_df.cpu.isin(cpus)]
                    os_freqs = os_df[os_df.cpu.isin(cpus)]
                    logger.debug("First freqs for %s:\n%s", cpus, dl_freqs)
                    # All devlib events "before" os-generated events
                    logger.debug("Min os freq @: %s", os_freqs.index.min())
                    if os_freqs.empty or \
                       os_freqs.index.min() > dl_freqs.index.max():
                        logger.debug("Insert devlib freqs for %s", cpus)
                        df = pd.concat([dl_freqs, df])

                # Inject "final" devlib frequencies
                os_df = df
                dl_df = devlib_freq.iloc[self.cpus_count:]
                for cpus in domains:
                    dl_freqs = dl_df[dl_df.cpu.isin(cpus)]
                    os_freqs = os_df[os_df.cpu.isin(cpus)]
                    logger.debug("Last freqs for %s:\n%s", cpus, dl_freqs)
                    # All devlib events "after" os-generated events
                    logger.debug("Max os freq @: %s", os_freqs.index.max())
                    if os_freqs.empty or \
                       os_freqs.index.max() < dl_freqs.index.min():
                        logger.debug("Append devlib freqs for %s", cpus)
                        df = pd.concat([df, dl_freqs])

                df.sort_index(inplace=True)

            setattr(self.ftrace.cpu_frequency, 'data_frame', df)

        # Frequency Coherency Check
        for cpus in domains:
            cluster_df = df[df.cpu.isin(cpus)]
            for chunk in self._chunker(cluster_df, len(cpus)):
                f = chunk.iloc[0].frequency
                if any(chunk.frequency != f):
                    logger.warning('Cluster Frequency is not coherent! '
                                      'Failure in [cpu_frequency] events at:')
                    logger.warning(chunk)
                    self.freq_coherency = False
                    return
        logger.info('Platform clusters verified to be Frequency coherent')

###############################################################################
# Utility Methods
###############################################################################

    def integrate_square_wave(self, sq_wave):
        """
        Compute the integral of a square wave time series.

        :param sq_wave: square wave assuming only 1.0 and 0.0 values
        :type sq_wave: :class:`pandas.Series`
        """
        sq_wave.iloc[-1] = 0.0
        # Compact signal to obtain only 1-0-1-0 sequences
        comp_sig = sq_wave.loc[sq_wave.shift() != sq_wave]
        # First value for computing the difference must be a 1
        if comp_sig.iloc[0] == 0.0:
            return sum(comp_sig.iloc[2::2].index - comp_sig.iloc[1:-1:2].index)
        else:
            return sum(comp_sig.iloc[1::2].index - comp_sig.iloc[:-1:2].index)

    def _loadFunctionsStats(self, path='trace.stats'):
        """
        Read functions profiling file and build a data frame containing all
        relevant data.

        :param path: path to the functions profiling trace file
        :type path: str
        """
        if os.path.isdir(path):
            path = os.path.join(path, 'trace.stats')
        if (path.endswith('dat') or
            path.endswith('txt') or
            path.endswith('html')):
            pre, ext = os.path.splitext(path)
            path = pre + '.stats'
        if not os.path.isfile(path):
            return False

        # Opening functions profiling JSON data file
        self.get_logger().debug('Loading functions profiling data from [%s]...', path)
        with open(os.path.join(path), 'r') as fh:
            trace_stats = json.load(fh)

        # Build DataFrame of function stats
        frames = {}
        for cpu, data in trace_stats.items():
            frames[int(cpu)] = pd.DataFrame.from_dict(data, orient='index')

        # Build and keep track of the DataFrame
        self._functions_stats_df = pd.concat(list(frames.values()),
                                             keys=list(frames.keys()))

        return len(self._functions_stats_df) > 0

    @memoized
    def getCPUActiveSignal(self, cpu):
        """
        Build a square wave representing the active (i.e. non-idle) CPU time,
        i.e.:

          cpu_active[t] == 1 if the CPU is reported to be non-idle by cpuidle at
          time t
          cpu_active[t] == 0 otherwise

        :param cpu: CPU ID
        :type cpu: int

        :returns: A :class:`pandas.Series` or ``None`` if the trace contains no
                  "cpu_idle" events
        """
        if not self.hasEvents('cpu_idle'):
            self.get_logger().warning('Events [cpu_idle] not found, '
                              'cannot compute CPU active signal!')
            return None

        idle_df = self.df_events('cpu_idle')
        cpu_df = idle_df[idle_df.cpu_id == cpu]

        cpu_active = cpu_df.state.apply(
            lambda s: 1 if s == NON_IDLE_STATE else 0
        )

        start_time = 0.0
        if not self.ftrace.normalized_time:
            start_time = self.ftrace.basetime

        if cpu_active.empty:
            cpu_active = pd.Series([0], index=[start_time])
        elif cpu_active.index[0] != start_time:
            entry_0 = pd.Series(cpu_active.iloc[0] ^ 1, index=[start_time])
            cpu_active = pd.concat([entry_0, cpu_active])

        # Fix sequences of wakeup/sleep events reported with the same index
        return handle_duplicate_index(cpu_active)

    @memoized
    def getPeripheralClockEffectiveRate(self, clk_name):
        logger = self.get_logger()
        if clk_name is None:
            logger.warning('no specified clk_name in computing peripheral clock, returning None')
            return
        if not self.hasEvents('clock_set_rate'):
            logger.warning('Events [clock_set_rate] not found, returning None!')
            return
        rate_df = self.df_events('clock_set_rate')
        enable_df = self.df_events('clock_enable')
        disable_df = self.df_events('clock_disable')
        pd.set_option('display.expand_frame_repr', False)

        freq = rate_df[rate_df.clk_name == clk_name]
        if not enable_df.empty:
            enables = enable_df[enable_df.clk_name == clk_name]
        if not disable_df.empty:
            disables = disable_df[disable_df.clk_name == clk_name]

        freq = pd.concat([freq, enables, disables], sort=False).sort_index()
        if freq.empty:
            logger.warning('No events for clock ' + clk_name + ' found in trace')
            return

        freq['start'] = freq.index
        freq['len'] = (freq.start - freq.start.shift()).fillna(0).shift(-1)
        # The last value will be NaN, fix to be appropriate length
        freq.loc[freq.index[-1], 'len'] = self.start_time + self.time_range - freq.index[-1]

        freq = freq.fillna(method='ffill')
        freq['effective_rate'] = np.where(freq['state'] == 0, 0,
                                          np.where(freq['state'] == 1, freq['rate'], float('nan')))
        return freq

    def add_events_deltas(self, df, col_name='delta', inplace=True):
        """
        Store the time between each event in a new dataframe column

        :param df: The DataFrame to operate one
        :type df: pandas.DataFrame

        :param col_name: The name of the column to add
        :type col_name: str

        :param inplace: Whether to operate on the passed DataFrame, or to use
          a copy of it
        :type inplace: bool

        This method only really makes sense for events tracking an on/off state
        (e.g. overutilized, idle)
        """
        if df.empty:
            return df

        if col_name in df.columns:
            raise RuntimeError("Column {} is already present in the dataframe".
                               format(col_name))

        if not inplace:
            df = df.copy()

        time_df = pd.DataFrame(index=df.index, data=df.index.values, columns=["start"])
        df[col_name] = (time_df.start - time_df.start.shift()).fillna(0).shift(-1)

        # Fix the last event, which will have a NaN duration
        # Set duration to trace_end - last_event
        df.loc[df.index[-1], col_name] = self.start_time + self.time_range - df.index[-1]

        return df

    @staticmethod
    def squash_df(df, start, end, column='delta'):
        """
        Slice a dataframe of deltas in [start:end] and ensure we have
        an event at exactly those boundaries.

        The input dataframe is expected to have a "column" which reports
        the time delta between consecutive rows, as for example dataframes
        generated by add_events_deltas().

        The returned dataframe is granted to have an initial and final
        event at the specified "start" ("end") index values, which values
        are the same of the last event before (first event after) the
        specified "start" ("end") time.

        Examples:

        Slice a dataframe to [start:end], and work on the time data so that it
        makes sense within the interval.

        Examples to make it clearer:

        df is:
        Time len state
        15    1   1
        16    1   0
        17    1   1
        18    1   0
        -------------

        slice_df(df, 16.5, 17.5) =>

        Time len state
        16.5  .5   0
        17    .5   1

        slice_df(df, 16.2, 16.8) =>

        Time len state
        16.2  .6   0

        :returns: a new df that fits the above description
        """
        if df.empty:
            return df

        end = min(end, df.index[-1] + df[column].values[-1])
        res_df = pd.DataFrame(data=[], columns=df.columns)

        if start > end:
            return res_df

        # There's a few things to keep in mind here, and it gets confusing
        # even for the people who wrote the code. Let's write it down.
        #
        # It's assumed that the data is continuous, i.e. for any row 'r' within
        # the trace interval, we will find a new row at (r.index + r.len)
        # For us this means we'll never end up with an empty dataframe
        # (if we started with a non empty one)
        #
        # What's we're manipulating looks like this:
        # (| = events; [ & ] = start,end slice)
        #
        # |   [   |   ]   |
        # e0  s0  e1  s1  e2
        #
        # We need to push e0 within the interval, and then tweak its duration
        # (len column). The mathemagical incantation for that is:
        # e0.len = min(e1.index - s0, s1 - s0)
        #
        # This takes care of the case where s1 isn't in the interval
        # If s1 is in the interval, we just need to cap its len to
        # s1 - e1.index

        prev_df = df[:start]
        middle_df = df[start:end]

        # Tweak the closest previous event to include it in the slice
        if not prev_df.empty and not (start in middle_df.index):
            res_df = res_df.append(prev_df.tail(1))
            res_df.index = [start]
            e1 = end

            if not middle_df.empty:
                e1 = middle_df.index[0]

            res_df[column] = min(e1 - start, end - start)

        if not middle_df.empty:
            res_df = res_df.append(middle_df)
            if end in res_df.index:
                # e_last and s1 collide, ditch e_last
                res_df = res_df.drop([end])
            else:
                # Fix the delta for the last row
                delta = min(end - res_df.index[-1], res_df[column].values[-1])
                res_df.at[res_df.index[-1], column] = delta

        return res_df

# vim :set tabstop=4 shiftwidth=4 expandtab textwidth=80
