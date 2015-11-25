from __future__ import print_function, division
from collections import OrderedDict, deque
from datetime import datetime
from warnings import warn

import pandas as pd

from nilmtk.feature_detectors.cluster import hart85_means_shift_cluster
from nilmtk.feature_detectors.steady_states import (
    find_steady_states_transients)
from nilmtk.timeframe import merge_timeframes, TimeFrame
from nilmtk.disaggregate import Disaggregator


# Fix the seed for repeatability of experiments
SEED = 42
import numpy as np

np.random.seed(SEED)


class MyDeque(deque):
    def popmiddle(self, pos):
        self.rotate(-pos)
        ret = self.popleft()
        self.rotate(pos)
        return ret


class PairBuffer(object):
    """
    Attributes:
    * transitionList (list of tuples)
    * matchedPairs (dataframe containing matched pairs of transitions)
    """

    def __init__(self, buffer_size, min_tolerance, percent_tolerance,
                 large_transition, num_measurements):
        """
        Parameters
        ----------
        buffer_size: int, optional
            size of the buffer to use for finding edges
        min_tolerance: int, optional
            variance in power draw allowed for pairing a match
        percent_tolerance: float, optional
            if transition is greater than large_transition, then use percent of large_transition
        large_transition: float, optional
            power draw of a Large transition
        num_measurements: int, optional
            2 if only active power
            3 if both active and reactive power
        """
        # We use a deque here, because it allows us quick access to start and end popping
        # and additionally, we can set a maxlen which drops oldest items. This nicely
        # suits Hart's recomendation that the size should be tunable.
        self._buffer_size = buffer_size
        self._min_tol = min_tolerance
        self._percent_tol = percent_tolerance
        self._large_transition = large_transition
        self.transition_list = MyDeque([], maxlen=self._buffer_size)
        self._num_measurements = num_measurements
        if self._num_measurements == 3:
            # Both active and reactive power is available
            self.pair_columns = ['T1 Time', 'T1 Active', 'T1 Reactive',
                                 'T2 Time', 'T2 Active', 'T2 Reactive']
        elif self._num_measurements == 2:
            # Only active power is available
            self.pair_columns = ['T1 Time', 'T1 Active',
                                 'T2 Time', 'T2 Active']
        self.matched_pairs = pd.DataFrame(columns=self.pair_columns)

    def clean_buffer(self):
        # Remove any matched transactions
        for idx, entry in enumerate(self.transition_list):
            if entry[self._num_measurements]:
                self.transition_list.popmiddle(idx)
                self.clean_buffer()
                break
                # Remove oldest transaction if buffer cleaning didn't remove anything
                # if len(self.transitionList) == self._bufferSize:
                #    self.transitionList.popleft()

    def add_transition(self, transition):
        # Check transition is as expected.
        assert isinstance(transition, (tuple, list))
        # Check that we have both active and reactive powers.
        assert len(transition) == self._num_measurements
        # Convert as appropriate
        if isinstance(transition, tuple):
            mtransition = list(transition)
        # Add transition to List of transitions (set marker as unpaired)
        mtransition.append(False)
        self.transition_list.append(mtransition)
        # checking for pairs
        # self.pairTransitions()
        # self.cleanBuffer()

    def pair_transitions(self):
        """
        Hart 85, P 33.
        When searching the working buffer for pairs, the order in which 
        entries are examined is very important. If an Appliance has 
        on and off several times in succession, there can be many 
        pairings between entries in the buffer. The algorithm must not
        allow an 0N transition to match an OFF which occurred at the end 
        of a different cycle, so that only ON/OFF pairs which truly belong 
        together are paired up. Otherwise the energy consumption of the 
        appliance will be greatly overestimated. The most straightforward 
        search procedures can make errors of this nature when faced with 
        types of transition sequences.

        Hart 85, P 32.
        For the two-state load monitor, a pair is defined as two entries
        which meet the following four conditions:
        (1) They are on the same leg, or are both 240 V,
        (2) They are both unmarked, 
        (3) The earlier has a positive real power component, and 
        (4) When added together, they result in a vector in which the 
        absolute value of the real power component is less than 35 
        Watts (or 3.5% of the real power, if the transitions are 
        over 1000 W) and the absolute value of the reactive power 
        component is less than 35 VAR (or 3.5%).

        ... the correct way to search the buffer is to start by checking 
        elements which are close together in the buffer, and gradually 
        increase the distance. First, adjacent  elements are checked for 
        pairs which meet all four requirements above; if any are found 
        they are processed and marked. Then elements two entries apart 
        are checked, then three, and so on, until the first and last 
        element are checked...

        """

        tlength = len(self.transition_list)
        pairmatched = False
        if tlength < 2:
            return pairmatched

        # Can we reduce the running time of this algorithm?
        # My gut feeling is no, because we can't re-order the list...
        # I wonder if we sort but then check the time... maybe. TO DO
        # (perhaps!).

        # Start the element distance at 1, go up to current length of buffer
        for eDistance in range(1, tlength):
            idx = 0
            while idx < tlength - 1:
                # We don't want to go beyond length of array
                compindex = idx + eDistance
                if compindex < tlength:
                    val = self.transition_list[idx]
                    # val[1] is the active power and
                    # val[self._num_measurements] is match status
                    if (val[1] > 0) and (val[self._num_measurements] is False):
                        compval = self.transition_list[compindex]
                        if compval[self._num_measurements] is False:
                            # Add the two elements for comparison
                            vsum = np.add(
                                val[1:self._num_measurements],
                                compval[1:self._num_measurements])
                            # Set the allowable tolerance for reactive and
                            # active
                            matchtols = [self._min_tol, self._min_tol]
                            for ix in range(1, self._num_measurements):
                                matchtols[ix - 1] = self._min_tol if (max(np.fabs([val[ix], compval[ix]]))
                                                                      < self._large_transition) else (self._percent_tol
                                                                                                      * max(
                                    np.fabs([val[ix], compval[ix]])))
                            if self._num_measurements == 3:
                                condition = (np.fabs(vsum[0]) < matchtols[0]) and (
                                    np.fabs(vsum[1]) < matchtols[1])

                            elif self._num_measurements == 2:
                                condition = np.fabs(vsum[0]) < matchtols[0]

                            if condition:
                                # Mark the transition as complete
                                self.transition_list[idx][
                                    self._num_measurements] = True
                                self.transition_list[compindex][
                                    self._num_measurements] = True
                                pairmatched = True

                                # Append the OFF transition to the ON. Add to
                                # dataframe.
                                matchedpair = val[
                                              0:self._num_measurements] + compval[0:self._num_measurements]
                                self.matched_pairs.loc[
                                    len(self.matched_pairs)] = matchedpair

                    # Iterate Index
                    idx += 1
                else:
                    break

        return pairmatched


class Hart85(Disaggregator):
    """1 or 2 dimensional Hart 1985 algorithm.

    Attributes
    ----------
    model : dict
        Each key is either the instance integer for an ElecMeter,
        or a tuple of instances for a MeterGroup.
        Each value is a sorted list of power in different states.
    """

    def __init__(self):
        self.model = {}
        self.MODEL_NAME = "Hart85"

    def train(self, metergroup, cols=[('power', 'active')],
              buffer_size=20, noise_level=70, state_threshold=15,
              min_tolerance=100, percent_tolerance=0.035,
              large_transition=1000, **kwargs):
        """
        Train using Hart85. Places the learnt model in `model` attribute.

        Parameters
        ----------
        metergroup : a nilmtk.MeterGroup object
        cols: nilmtk.Measurement, should be one of the following
            [('power','active')]
            [('power','apparent')]
            [('power','reactive')]
            [('power','active'), ('power', 'reactive')]
        buffer_size: int, optional
            size of the buffer to use for finding edges
        min_tolerance: int, optional
            variance in power draw allowed for pairing a match
        percent_tolerance: float, optional
            if transition is greater than large_transition,
            then use percent of large_transition
        large_transition: float, optional
            power draw of a Large transition
        """
        self.cols = cols
        self.state_threshold = state_threshold
        self.noise_level = noise_level
        [self.steady_states, self.transients] = find_steady_states_transients(
            metergroup, cols, noise_level, state_threshold, **kwargs)
        self.pair_df = self.pair(
            buffer_size, min_tolerance, percent_tolerance, large_transition)
        self.centroids = hart85_means_shift_cluster(self.pair_df, cols)

    def pair(self, buffer_size, min_tolerance, percent_tolerance,
             large_transition):
        subset = list(self.transients.itertuples())
        buffer = PairBuffer(
            min_tolerance=min_tolerance, buffer_size=buffer_size,
            percent_tolerance=percent_tolerance,
            large_transition=large_transition,
            num_measurements=len(self.transients.columns) + 1)
        for s in subset:
            # if len(buffer.transitionList) < bsize
            if len(buffer.transition_list) == buffer_size:
                buffer.clean_buffer()
            buffer.add_transition(s)
            buffer.pair_transitions()
        return buffer.matched_pairs

    def disaggregate_chunk(self, chunk, prev, transients):
        """
        Parameters
        ----------
        chunk : pd.DataFrame
            mains power
        prev
        transients : returned by find_steady_state_transients

        Returns
        -------
        states : pd.DataFrame
            with same index as `chunk`.
        """

        states = pd.DataFrame(
            -1, index=chunk.index, columns=self.centroids.index.values)
        for transient_tuple in transients.itertuples():
            if transient_tuple[0] < chunk.index[0]:
                # Transient occurs before chunk has started; do nothing
                pass
            elif transient_tuple[0] > chunk.index[-1]:
                # Transient occurs after chunk has ended; do nothing
                pass
            else:
                # Absolute value of transient
                abs_value = np.abs(transient_tuple[1:])
                positive = transient_tuple[1] > 0
                abs_value_transient_minus_centroid = pd.DataFrame(
                    (self.centroids - abs_value).abs())
                if len(transient_tuple) == 2:
                    # 1d data
                    index_least_delta = (
                        abs_value_transient_minus_centroid.idxmin().values[0])
                else:
                    # 2d data.
                    # Need to find absolute value before computing minimum
                    columns = abs_value_transient_minus_centroid.columns
                    abs_value_transient_minus_centroid["multidim"] = (
                        abs_value_transient_minus_centroid[columns[0]] ** 2
                        +
                        abs_value_transient_minus_centroid[columns[1]] ** 2)
                    index_least_delta = (
                        abs_value_transient_minus_centroid["multidim"].argmin())
                if positive:
                    # Turned on
                    states.loc[transient_tuple[0]][index_least_delta] = 1
                else:
                    # Turned off
                    states.loc[transient_tuple[0]][index_least_delta] = 0
        prev = states.iloc[-1].to_dict()
        power_chunk_dict = self.assign_power_from_states(states, prev)
        return pd.DataFrame(power_chunk_dict, index=chunk.index)

    def assign_power_from_states(self, states_chunk, prev):
        di = {}
        ndim = len(self.centroids.columns)
        for appliance in states_chunk.columns:
            values = states_chunk[[appliance]].values.flatten()
            if ndim == 1:
                power = np.zeros(len(values), dtype=int)
            else:
                power = np.zeros((len(values), 2), dtype=int)
            # on = False
            i = 0
            while i < len(values) - 1:
                if values[i] == 1:
                    # print("A", values[i], i)
                    on = True
                    i = i + 1
                    power[i] = self.centroids.ix[appliance].values
                    while values[i] != 0 and i < len(values) - 1:
                        # print("B", values[i], i)
                        power[i] = self.centroids.ix[appliance].values
                        i = i + 1
                elif values[i] == 0:
                    # print("C", values[i], i)
                    on = False
                    i = i + 1
                    power[i] = 0
                    while values[i] != 1 and i < len(values) - 1:
                        # print("D", values[i], i)
                        if ndim == 1:
                            power[i] = 0
                        else:
                            power[i] = [0, 0]
                        i = i + 1
                else:
                    # print("E", values[i], i)
                    # Unknown state. If previously we know about this
                    # appliance's state, we can
                    # use that. Else, it defaults to 0
                    if prev[appliance] == -1 or prev[appliance] == 0:
                        # print("F", values[i], i)
                        on = False
                        power[i] = 0
                        while values[i] != 1 and i < len(values) - 1:
                            # print("G", values[i], i)
                            if ndim == 1:
                                power[i] = 0
                            else:
                                power[i] = [0, 0]
                            i = i + 1
                    else:
                        # print("H", values[i], i)
                        on = True
                        power[i] = self.centroids.ix[appliance].values
                        while values[i] != 0 and i < len(values) - 1:
                            # print("I", values[i], i)
                            power[i] = self.centroids.ix[appliance].values
                            i = i + 1

            di[appliance] = power
            # print(power.sum())
        return di

    def disaggregate(self, mains, output_datastore, **load_kwargs):
        """Disaggregate mains according to the model learnt previously.

        Parameters
        ----------
        mains : nilmtk.ElecMeter or nilmtk.MeterGroup
        output_datastore : instance of nilmtk.DataStore subclass
            For storing power predictions from disaggregation algorithm.
        sample_period : number, optional
            The desired sample period in seconds.
        **load_kwargs : key word arguments
            Passed to `mains.power_series(**kwargs)`
        """
        load_kwargs = self._pre_disaggregation_checks(load_kwargs)

        load_kwargs.setdefault('sample_period', 60)
        load_kwargs.setdefault('sections', mains.good_sections())

        timeframes = []
        building_path = '/building{}'.format(mains.building())
        mains_data_location = building_path + '/elec/meter1'
        data_is_available = False

        [_, transients] = find_steady_states_transients(
            mains, cols=self.cols, state_threshold=self.state_threshold,
            noise_level=self.noise_level, **load_kwargs)

        # For now ignoring the first transient
        # transients = transients[1:]

        # Initially all appliances/meters are in unknown state (denoted by -1)
        prev = OrderedDict()
        learnt_meters = self.centroids.index.values
        for meter in learnt_meters:
            prev[meter] = -1

        timeframes = []
        # Now iterating over mains data and disaggregating chunk by chunk
        for chunk in mains.power_series(**load_kwargs):
            # Record metadata
            timeframes.append(chunk.timeframe)
            measurement = chunk.name
            power_df = self.disaggregate_chunk(
                chunk, prev, transients)

            cols = pd.MultiIndex.from_tuples([chunk.name])

            for meter in learnt_meters:
                data_is_available = True
                df = power_df[[meter]]
                df.columns = cols
                key = '{}/elec/meter{:d}'.format(building_path, meter + 2)
                output_datastore.append(key, df)

            output_datastore.append(key=mains_data_location,
                                    value=pd.DataFrame(chunk, columns=cols))

        if data_is_available:
            self._save_metadata_for_disaggregation(
                output_datastore=output_datastore,
                sample_period=load_kwargs['sample_period'],
                measurement=measurement,
                timeframes=timeframes,
                building=mains.building(),
                supervised=False,
                num_meters=len(self.centroids)
            )

    """
    def export_model(self, filename):
        model_copy = {}
        for appliance, appliance_states in self.model.iteritems():
            model_copy[
                "{}_{}".format(appliance.name, appliance.instance)] = appliance_states
        j = json.dumps(model_copy)
        with open(filename, 'w+') as f:
            f.write(j)

    def import_model(self, filename):
        with open(filename, 'r') as f:
            temp = json.loads(f.read())
        for appliance, centroids in temp.iteritems():
            appliance_name = appliance.split("_")[0].encode("ascii")
            appliance_instance = int(appliance.split("_")[1])
            appliance_name_instance = ApplianceID(
                appliance_name, appliance_instance)
            self.model[appliance_name_instance] = centroids
    """
