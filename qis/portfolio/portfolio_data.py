from __future__ import annotations

# packages
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt
from numba import njit
from dataclasses import dataclass
from typing import Union, Dict, Any, Optional, Tuple, List, NamedTuple
from enum import Enum

# qis
import qis as qis
import qis.file_utils as fu
import qis.utils.dates as da
import qis.utils.df_groups as dfg
import qis.utils.df_agg as dfa
import qis.utils.struct_ops as sop
import qis.perfstats.returns as ret
import qis.perfstats.perf_stats as rpt
import qis.perfstats.regime_classifier as rcl
from qis import (PerfStat, PerfParams, RegimeData, EnumMap, BenchmarkReturnsQuantileRegimeSpecs,
                 TimePeriod, RollingPerfStat)

# plots
import qis.plots.time_series as pts
import qis.plots.stackplot as pst
import qis.plots.derived.prices as ppd
import qis.plots.derived.perf_table as ppt
import qis.plots.derived.returns_scatter as prs
import qis.plots.derived.returns_heatmap as rhe
from qis.plots.derived.returns_heatmap import plot_returns_heatmap
import qis.models.linear.ewm_factors as ef


PERF_PARAMS = PerfParams(freq='W-WED')
REGIME_PARAMS = BenchmarkReturnsQuantileRegimeSpecs(freq='ME')


class MetricSpec(NamedTuple):
    title: str


class AttributionMetric(MetricSpec, Enum):
    PNL = MetricSpec(title='P&L Attribution, sum=portfolio performance')
    PNL_RISK = MetricSpec(title='P&L Risk Attribution, sum=100%')
    INST_PNL = MetricSpec(title='Instrument P&L')


@dataclass
class PortfolioData:
    nav: pd.Series  # nav is computed with all cost
    weights: pd.DataFrame = None  # weights of portfolio
    units: pd.DataFrame = None  # units of portfolio instruments
    prices: pd.DataFrame = None  # prices of portfolio universe
    instrument_pnl: pd.DataFrame = None  # include net pnl by intsrument
    realized_costs: pd.DataFrame = None  # realized trading costs by instrument
    input_weights: Union[np.ndarray, pd.DataFrame, Dict[str, float]] = None  # inputs to potfolio
    is_rebalancing: pd.Series = None  # optional rebal info
    tickers_to_names_map: Optional[Dict[str, str]] = None  # renaming of long tickers
    group_data: pd.Series = None  # for asset class grouping
    group_order: List[str] = None
    benchmark_prices: pd.DataFrame = None  # can pass benchmark prices here

    def __post_init__(self):

        if isinstance(self.nav, pd.DataFrame):
            self.nav = self.nav.iloc[:, 0]
        if self.prices is None:
            self.prices = self.nav.to_frame()
        if self.weights is None:  # default will be delta-1 portfolio of nav
            self.weights = pd.DataFrame(1.0, index=self.prices.index, columns=self.prices.columns)
        if self.units is None:  # default will be delta-1 portfolio of nav
            self.units = pd.DataFrame(1.0, index=self.prices.index, columns=self.prices.columns)
        if self.instrument_pnl is None:
            self.instrument_pnl = self.prices.pct_change(fill_method=None).multiply(self.weights.shift(1)).fillna(0.0)
        if self.realized_costs is None:
            self.realized_costs = pd.DataFrame(0.0, index=self.prices.index, columns=self.prices.columns)
        if self.group_data is None:  # use instruments as groups
            self.group_data = pd.Series(self.prices.columns, index=self.prices.columns)
        if self.group_order is None:
            self.group_order = list(self.group_data.unique())
        if self.benchmark_prices is not None:
            self.benchmark_prices = self.benchmark_prices.reindex(index=self.nav.index, method='ffill')

    def set_benchmark_prices(self, benchmark_prices: Union[pd.Series, pd.DataFrame]) -> None:
        # can pass benchmark prices here
        if isinstance(benchmark_prices, pd.Series):
            benchmark_prices = benchmark_prices.to_frame()
        self.benchmark_prices = benchmark_prices.reindex(index=self.nav.index, method='ffill')

    def set_group_data(self, group_data: pd.Series, group_order: List[str] = None) -> None:
        self.group_data = group_data  # for asset class grouping
        if group_order is not None:
            self.group_order = group_order

    def save(self, ticker: str, local_path: str = './') -> None:
        datasets = dict(nav=self.nav, prices=self.prices, weights=self.weights, units=self.units,
                        instrument_pnl=self.instrument_pnl, realized_costs=self.realized_costs)
        if self.group_data is not None:
            datasets['group_data'] = self.group_data
        fu.save_df_dict_to_csv(datasets=datasets, file_name=ticker, local_path=local_path)
        print(f"saved portfolio data for {ticker}")

    @classmethod
    def load(cls, ticker: str) -> PortfolioData:
        dataset_keys = ['nav', 'prices', 'weights', 'units', 'instrument_pnl', 'realized_costs', 'group_data']
        datasets = fu.load_df_dict_from_csv(dataset_keys=dataset_keys, file_name=ticker)
        return cls(**datasets)

    def set_group_data(self, group_data: pd.Series, group_order: List[str] = None) -> None:
        self.group_data = group_data
        if group_order is None:
            group_order = list(group_data.unique())
        self.group_order = group_order

    """
    NAV level getters
    """

    def get_portfolio_nav(self, time_period: da.TimePeriod = None, freq: Optional[str] = None) -> pd.Series:
        """
        get nav using consistent function for all return computations
        """
        if time_period is not None:
            nav_ = time_period.locate(self.nav)
        else:
            nav_ = self.nav.copy()
        if freq is not None:
            nav_ = nav_.asfreq(freq=freq, method='ffill')
        return nav_

    def get_portfolio_nav_with_benchmark_rices(self,
                                               time_period: da.TimePeriod = None,
                                               freq: Optional[str] = None
                                               ) -> pd.DataFrame:
        """
        get nav using consistent function for all return computations
        """
        navs = self.get_portfolio_nav(time_period=time_period, freq=freq)
        if self.benchmark_prices is not None:
            benchmark_prices = self.benchmark_prices.reindex(index=navs.index, method='ffill')
            navs = pd.concat([navs, benchmark_prices], axis=1)
        return navs

    def get_instruments_pnl(self,
                            add_total: bool = False,
                            time_period: da.TimePeriod = None,
                            is_compounded: bool = False
                            ) -> pd.DataFrame:
        pnl = self.instrument_pnl.copy()
        if add_total:
            pnl.insert(loc=0, value=pnl.sum(1), column='Total')
        if time_period is not None:
            pnl = time_period.locate(pnl)
        if is_compounded:
            pnl = np.expm1(pnl)
        return pnl

    def get_performance_attribution(self,
                                    add_total: bool = True,
                                    time_period: da.TimePeriod = None,
                                    is_compounded: bool = False
                                    ) -> pd.Series:
        instrument_pnl = self.get_instruments_pnl(add_total=add_total,
                                                  time_period=time_period,
                                                  is_compounded=is_compounded)
        if is_compounded:
            performance_attribution = instrument_pnl.iloc[-1, :] - 1.0
        else:
            performance_attribution = instrument_pnl.sum(axis=0)
        return performance_attribution

    def get_instruments_navs(self,
                             time_period: da.TimePeriod = None,
                             constant_trade_level: bool = False
                             ) -> pd.DataFrame:
        pnl = self.get_instruments_pnl(time_period=time_period, is_compounded=False).fillna(0.0)
        navs = ret.returns_to_nav(returns=pnl, constant_trade_level=constant_trade_level)
        return navs

    def get_group_navs(self,
                    time_period: da.TimePeriod = None,
                    constant_trade_level: bool = False
                    ) -> pd.DataFrame:
        grouped_pnl = dfg.agg_df_by_groups_ax1(df=self.get_instruments_pnl(time_period=time_period),
                                                  group_data=self.group_data,
                                                  agg_func=np.sum,
                                                  total_column=str(self.nav.name),
                                                  group_order=self.group_order)
        group_navs = ret.returns_to_nav(returns=grouped_pnl, constant_trade_level=constant_trade_level)
        return group_navs

    def get_weights(self,
                    is_input_weights: bool = True,
                    columns: List[str] = None,
                    time_period: TimePeriod = None,
                    freq: Optional[str] = 'W-WED'
                    ) -> pd.DataFrame:
        if is_input_weights and self.input_weights is not None:
            weights = self.input_weights.copy()
        else:
            weights = self.weights.copy()
        if columns is not None:
            weights = weights[columns]
        if time_period is not None:
            weights = time_period.locate(weights)
        if freq is not None:
            weights = weights.resample(freq).last().ffill()
        return weights

    def get_exposures(self,
                      time_period: da.TimePeriod = None,
                      is_grouped: bool = False,
                      add_total: bool = True
                      ) -> pd.DataFrame:
        if is_grouped:
            exposures = dfg.agg_df_by_groups_ax1(df=self.weights,
                                                 group_data=self.group_data,
                                                 agg_func=np.nansum,
                                                 total_column=str(self.nav.name) if add_total else None,
                                                 group_order=self.group_order)
        else:
            exposures = self.weights
        if time_period is not None:
            exposures = time_period.locate(exposures)
        return exposures

    def get_grouped_exposures(self, time_period: da.TimePeriod = None
                              ) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
        """
        compute grouped net long / short exposues
        """
        group_dict = dfg.get_group_dict(group_data=self.group_data,
                                        group_order=self.group_order,
                                        total_column=None)
        all_exposures = self.get_exposures(time_period=time_period)
        grouped_exposures_by_inst = {}
        grouped_exposures_agg = {}
        for group, tickers in group_dict.items():
            exposures_by_inst = all_exposures[tickers]
            grouped_exposures_by_inst[group] = exposures_by_inst
            total = dfa.nansum(exposures_by_inst, axis=1).rename('Total')
            net_long = dfa.nansum_positive(exposures_by_inst, axis=1).rename('Net Long')
            net_short = dfa.nansum_negative(exposures_by_inst, axis=1).rename('Net Short')
            grouped_exposures_agg[group] = pd.concat([total, net_long, net_short], axis=1)
        return grouped_exposures_agg, grouped_exposures_by_inst

    def get_grouped_cum_pnls(self, time_period: da.TimePeriod = None
                             ) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
        """
        compute grouped net long / short exposues
        """
        group_dict = dfg.get_group_dict(group_data=self.group_data,
                                        group_order=self.group_order,
                                        total_column=None)

        pnls = self.get_instruments_pnl(time_period=time_period, is_compounded=False).fillna(0.0)
        all_exposures = self.get_exposures(time_period=time_period)
        pnl_positive_exp = pnls.where(all_exposures > 0.0, other=0.0)
        pnl_negative_exp = pnls.where(all_exposures < 0.0, other=0.0)

        grouped_pnls_by_inst = {}
        grouped_pnls_agg = {}
        for group, tickers in group_dict.items():
            pnls_by_inst = pnls[tickers]
            grouped_pnls_by_inst[group] = pnls_by_inst.cumsum(axis=0)
            total = dfa.nansum(pnls_by_inst, axis=1).rename('Total')
            net_long = dfa.nansum(pnl_positive_exp[tickers], axis=1).rename('Net Long')
            net_short = dfa.nansum(pnl_negative_exp[tickers], axis=1).rename('Net Short')
            grouped_pnls_agg[group] = pd.concat([total, net_long, net_short], axis=1).cumsum(axis=0)
        return grouped_pnls_agg, grouped_pnls_by_inst

    def get_turnover(self,
                     is_agg: bool = False,
                     is_grouped: bool = False,
                     time_period: da.TimePeriod = None,
                     roll_period: Optional[int] = 260,
                     add_total: bool = True,
                     freq: Optional[str] = None
                     ) -> Union[pd.DataFrame, pd.Series]:
        turnover = (self.units.diff(1).abs()).multiply(self.prices)
        abs_exposure = self.units.multiply(self.prices).abs().sum(axis=1)
        # turnover = turnover.divide(self.nav.to_numpy(), axis=0)
        turnover = turnover.divide(abs_exposure.to_numpy(), axis=0)
        if is_agg:
            turnover = pd.Series(np.nansum(turnover, axis=1), index=turnover.index, name=self.nav.name)
            turnover = turnover.reindex(index=self.nav.index)
        elif is_grouped or len(turnover.columns) > 10:  # agg by groups
            turnover = dfg.agg_df_by_groups_ax1(df=turnover,
                                                group_data=self.group_data,
                                                agg_func=np.nansum,
                                                total_column=str(self.nav.name) if add_total else None,
                                                group_order=self.group_order)
        else:
            if add_total:
                turnover = pd.concat([turnover.sum(axis=1).rename(self.nav.name), turnover], axis=1)

        if roll_period is not None:
            turnover = turnover.rolling(roll_period).sum()
        elif freq is not None:
            turnover = turnover.resample(freq).sum()
        if time_period is not None:
            turnover = time_period.locate(turnover)
        return turnover

    def get_costs(self,
                  is_agg: bool = False,
                  is_grouped: bool = False,
                  time_period: da.TimePeriod = None,
                  add_total: bool = True,
                  is_norm_costs: bool = True,
                  roll_period: Optional[int] = 260
                  ) -> Union[pd.DataFrame, pd.Series]:

        costs = self.realized_costs
        if is_norm_costs:
            costs = costs.divide(self.nav.to_numpy(), axis=0)
        if is_agg:
            costs = pd.Series(np.nansum(costs, axis=1), index=self.nav.index, name=self.nav.name)
        elif is_grouped:
            costs = dfg.agg_df_by_groups_ax1(costs,
                                             group_data=self.group_data,
                                             agg_func=np.nansum,
                                             total_column=str(self.nav.name) if add_total else None,
                                             group_order=self.group_order)
        else:
            if add_total:
                costs = pd.concat([costs.sum(axis=1).rename(self.nav.name), costs], axis=1)

        if roll_period is not None:
            costs = costs.rolling(roll_period).sum()
        if time_period is not None:
            costs = time_period.locate(costs)
        return costs

    def compute_mcap_participation(self,
                                   mcap: pd.DataFrame,
                                   trade_level: float = 100000000
                                   ) -> pd.DataFrame:
        exposure = (self.units.multiply(self.prices)).divide(self.nav.to_numpy(), axis=0)
        participation = trade_level * exposure.divide(mcap)
        return participation

    def compute_volume_participation(self,
                                     volumes: pd.DataFrame,
                                     trade_level: float = 100000000
                                     ) -> pd.DataFrame:
        turnover = self.get_turnover(is_agg=False)
        participation = trade_level * turnover.divide(volumes)
        return participation

    def compute_cumulative_attribution(self) -> pd.DataFrame:
        attribution = (self.prices.pct_change()).multiply(self.weights.shift(1))
        attribution = attribution.cumsum(axis=0)
        return attribution

    def compute_realized_pnl(self, time_period: da.TimePeriod = None) -> Tuple[pd.DataFrame, ...]:
        avg_costs, realized_pnl, mtm_pnl, trades = compute_realized_pnl(prices=self.prices.to_numpy(),
                                                                        units=self.units.to_numpy())
        avg_costs = pd.DataFrame(avg_costs, index=self.prices.index, columns=self.prices.columns)
        realized_pnl = pd.DataFrame(realized_pnl, index=self.prices.index, columns=self.prices.columns)
        mtm_pnl = pd.DataFrame(mtm_pnl, index=self.prices.index, columns=self.prices.columns)
        trades = pd.DataFrame(trades, index=self.prices.index, columns=self.prices.columns)
        if time_period is not None:
            avg_costs = time_period.locate(avg_costs)
            realized_pnl = time_period.locate(realized_pnl)
            mtm_pnl = time_period.locate(mtm_pnl)
            trades = time_period.locate(trades)
        realized_pnl = realized_pnl.cumsum(axis=0)
        total_pnl = realized_pnl.add(mtm_pnl)
        return avg_costs, realized_pnl, mtm_pnl, total_pnl, trades

    def compute_portfolio_benchmark_betas(self, benchmark_prices: pd.DataFrame,
                                          time_period: da.TimePeriod = None,
                                          freq: str = None,
                                          span: int = 65  # quarter
                                          ) -> pd.DataFrame:
        instrument_prices = self.prices
        benchmark_prices = benchmark_prices.reindex(index=instrument_prices.index, method='ffill')
        ewm_linear_model = ef.estimate_ewm_linear_model(x=ret.to_returns(prices=benchmark_prices, freq=freq, is_log_returns=True),
                                                        y=ret.to_returns(prices=instrument_prices, freq=freq, is_log_returns=True),
                                                        span=span,
                                                        is_x_correlated=True)
        exposures = self.get_exposures().reindex(index=instrument_prices.index, method='ffill')
        benchmark_betas = ewm_linear_model.compute_agg_factor_exposures(asset_exposures=exposures)
        benchmark_betas = benchmark_betas.replace({0.0: np.nan}).ffill()  # fillholidays
        if time_period is not None:
            benchmark_betas = time_period.locate(benchmark_betas)
        return benchmark_betas

    def compute_portfolio_benchmark_attribution(self,
                                                benchmark_prices: pd.DataFrame,
                                                time_period: da.TimePeriod = None,
                                                freq: str = 'B',
                                                span: int = 63  # quarter
                                                ) -> pd.DataFrame:
        portfolio_benchmark_betas = self.compute_portfolio_benchmark_betas(benchmark_prices=benchmark_prices,
                                                                           freq=freq, span=span)
        benchmark_prices = benchmark_prices.reindex(index=portfolio_benchmark_betas.index, method='ffill')
        x = ret.to_returns(prices=benchmark_prices, freq=freq)
        x_attribution = (portfolio_benchmark_betas.shift(1)).multiply(x)
        total_attrib = x_attribution.sum(1)
        total = self.get_portfolio_nav().reindex(index=total_attrib.index, method='ffill').pct_change()
        residual = np.subtract(total, total_attrib)
        # joint_attrib = pd.concat([x_attribution, total_attrib.rename('Total benchmarks'),
        # residual.rename('Residual')], axis=1)
        joint_attrib = pd.concat([x_attribution, residual.rename('Residual')], axis=1)
        if time_period is not None:
            joint_attrib = time_period.locate(joint_attrib)
        joint_attrib = joint_attrib.cumsum(axis=0)
        return joint_attrib

    """
    ### instrument level getters
    """

    def get_instruments_returns(self,
                                time_period: da.TimePeriod = None
                                ) -> pd.DataFrame:
        returns = self.prices.pct_change(fill_method=None)
        if time_period is not None:
            returns = time_period.locate(returns)
        return returns

    def get_instruments_periodic_returns(self,
                                         time_period: da.TimePeriod = None,
                                         freq: str = 'ME'
                                         ) -> pd.DataFrame:
        returns = self.get_instruments_returns(time_period=time_period)
        prices = ret.returns_to_nav(returns=returns, init_period=None)
        returns_f = ret.to_returns(prices=prices, freq=freq)
        return returns_f

    def get_instruments_performance_attribution(self,
                                                time_period: da.TimePeriod = None,
                                                constant_trade_level: bool = False
                                                ) -> pd.Series:
        navs = self.get_instruments_navs(time_period=time_period, constant_trade_level=constant_trade_level)
        perf = ret.to_total_returns(prices=navs).rename(self.nav.name)
        return perf

    def get_instruments_pnl_risk_attribution(self,
                                             time_period: da.TimePeriod = None
                                             ) -> pd.DataFrame:
        pnl = self.get_instruments_pnl(time_period=time_period)
        # portfolio_pnl = pnl.sum(axis=1)

        pnl_risk = np.nanstd(pnl.replace({0.0: np.nan}), axis=0)
        # portfolio_pnl_risk = np.nanstd(portfolio_pnl.replace({0.0: np.nan}), axis=0)
        # pnl_risk_ratio = pnl_risk / portfolio_pnl_risk
        pnl_risk_ratio = pnl_risk / np.nansum(pnl_risk)

        data = pd.Series(pnl_risk_ratio, index=pnl.columns, name=self.nav.name)
        if self.tickers_to_names_map is not None:
            data = data.rename(index=self.tickers_to_names_map)
        return data

    def get_performance_data(self,
                             attribution_metric: AttributionMetric = AttributionMetric.PNL,
                             time_period: da.TimePeriod = None
                             ) -> Union[pd.DataFrame, pd.Series]:
        if attribution_metric == AttributionMetric.PNL:
            data = self.get_instruments_performance_attribution(time_period=time_period)
        elif attribution_metric == AttributionMetric.PNL_RISK:
            data = self.get_instruments_pnl_risk_attribution(time_period=time_period)
        elif attribution_metric == AttributionMetric.INST_PNL:
            data = self.get_instruments_navs(time_period=time_period)
        else:
            raise NotImplementedError(f"{attribution_metric}")

        return data

    def get_num_investable_instruments(self, time_period: da.TimePeriod = None) -> pd.Series:
        exposures = self.weights.replace({0.0: np.nan})
        count = np.sum(np.where(np.isfinite(exposures), 1.0, 0.0), axis=1)
        num_investable_instruments = pd.Series(count, index=exposures.index, name=self.nav.name)
        if time_period is not None:
            num_investable_instruments = time_period.locate(num_investable_instruments)
        return num_investable_instruments

    def get_instruments_performance_table(self,
                                          time_period: da.TimePeriod = None,
                                          portfolio_name: str = 'Attribution'
                                          ) -> pd.DataFrame:
        """
        using avg weight
        """
        insts_returns = self.get_instruments_returns(time_period=time_period)
        insts_return = ret.to_total_returns(prices=ret.returns_to_nav(returns=insts_returns))
        weight = self.weights
        if time_period is not None:
            weight = time_period.locate(weight)
        weight = weight.mean(axis=0)
        portf_return = insts_return.multiply(weight).replace({0.0: np.nan}).dropna()
        data = pd.concat([weight.rename('Weight'),
                          insts_return.rename('Asset'),
                          portf_return.rename(portfolio_name)],
                         axis=1).dropna()
        data = data.sort_values('Weight', ascending=False)
        if self.tickers_to_names_map is not None:
            data = data.rename(index=self.tickers_to_names_map)

        return data

    def get_attribution_table_by_instrument(self,
                                            time_period: da.TimePeriod = None,
                                            freq: str = 'ME',
                                            ) -> pd.DataFrame:
        """
        using avg weight
        """
        returns_f = self.get_instruments_periodic_returns(time_period=time_period, freq=freq)
        weight = self.weights.reindex(index=returns_f.index, method='ffill').shift(1)
        # first row is None
        portf_return = returns_f.multiply(weight).iloc[1:, :]
        if self.tickers_to_names_map is not None:
            portf_return = portf_return.rename(columns=self.tickers_to_names_map)
        return portf_return

    """
    plotting methods
    """
    def add_regime_shadows(self,
                           ax: plt.Subplot,
                           regime_benchmark: str,
                           index: pd.Index = None,
                           regime_params: BenchmarkReturnsQuantileRegimeSpecs = REGIME_PARAMS
                           ) -> None:
        """
        add regime shadows using regime_benchmark
        """
        if self.benchmark_prices is None:
            raise ValueError(f"set benchmarks data")
        pivot_prices = self.benchmark_prices[regime_benchmark]
        if index is not None:
            pivot_prices = pivot_prices.reindex(index=index, method='ffill')
        qis.add_bnb_regime_shadows(ax=ax, pivot_prices=pivot_prices, regime_params=regime_params)

    def plot_nav(self,
                 regime_benchmark: str = None,
                 time_period: da.TimePeriod = None,
                 add_benchmarks: bool = False,
                 regime_params: BenchmarkReturnsQuantileRegimeSpecs = REGIME_PARAMS,
                 ax: plt.Subplot = None,
                 **kwargs
                 ) -> None:
        if add_benchmarks:
            prices = self.get_portfolio_nav_with_benchmark_rices(time_period=time_period)
        else:
            prices = self.get_portfolio_nav(time_period=time_period)
        if ax is None:
            with sns.axes_style('darkgrid'):
                fig, ax = plt.subplots(1, 1, figsize=(16, 12), tight_layout=True)
        ppd.plot_prices(prices=prices, ax=ax, **kwargs)

        if regime_benchmark is not None:
            self.add_regime_shadows(ax=ax, regime_benchmark=regime_benchmark, index=prices.index,
                                    regime_params=regime_params)

    def plot_group_nav(self,
                       regime_benchmark: str = None,
                       time_period: da.TimePeriod = None,
                       add_benchmarks: bool = False,
                       regime_params: BenchmarkReturnsQuantileRegimeSpecs = REGIME_PARAMS,
                       ax: plt.Subplot = None,
                       **kwargs
                       ) -> None:
        
        group_navs = self.get_group_navs(time_period=time_period)
        if add_benchmarks and self.benchmark_prices is not None:
            benchmark_prices = self.benchmark_prices.reindex(index=group_navs.index, method='ffill')
            group_navs = pd.concat([group_navs, benchmark_prices], axis=1)
        if ax is None:
            with sns.axes_style('darkgrid'):
                fig, ax = plt.subplots(1, 1, figsize=(16, 12), tight_layout=True)
        ppd.plot_prices(prices=group_navs, ax=ax, **kwargs)

        if regime_benchmark is not None:
            self.add_regime_shadows(ax=ax, regime_benchmark=regime_benchmark, index=group_navs.index,
                                    regime_params=regime_params)
            
    def plot_rolling_perf(self,
                          rolling_perf_stat: RollingPerfStat = RollingPerfStat.SHARPE,
                          add_benchmarks: bool = False,
                          regime_benchmark: str = None,
                          time_period: TimePeriod = None,
                          rolling_window: int = 1300,
                          roll_freq: Optional[str] = None,
                          legend_stats: pts.LegendStats = pts.LegendStats.AVG_LAST,
                          title: Optional[str] = None,
                          var_format: str = '{:.2f}',
                          regime_params: BenchmarkReturnsQuantileRegimeSpecs = REGIME_PARAMS,
                          ax: plt.Subplot = None,
                          **kwargs
                          ) -> plt.Figure:

        # do not use start end dates here so the sharpe will be continuous with different time_period
        if add_benchmarks:
            prices = self.get_portfolio_nav_with_benchmark_rices(time_period=time_period)
        else:
            prices = self.get_portfolio_nav(time_period=time_period)

        if ax is None:
            fig, ax = plt.subplots()

        fig = ppd.plot_rolling_perf_stat(prices=prices,
                                         rolling_perf_stat=rolling_perf_stat,
                                         time_period=time_period,
                                         roll_periods=rolling_window,
                                         roll_freq=roll_freq,
                                         legend_stats=legend_stats,
                                         trend_line=qis.TrendLine.ZERO_SHADOWS,
                                         var_format=var_format,
                                         title=title or f"5y rolling Sharpe ratio",
                                         ax=ax,
                                         **kwargs)
        if regime_benchmark is not None:
            self.add_regime_shadows(ax=ax, regime_benchmark=regime_benchmark, index=prices.index,
                                    regime_params=regime_params)
        return fig

    def plot_ra_perf_table(self,
                           benchmark_price: pd.Series = None,
                           is_grouped: bool = True,
                           time_period: da.TimePeriod = None,
                           perf_params: PerfParams = None,
                           perf_columns: List[PerfStat] = rpt.BENCHMARK_TABLE_COLUMNS,
                           title: str = None,
                           ax: plt.Subplot = None,
                           **kwargs
                           ) -> None:
        if is_grouped:
            prices = self.get_group_navs(time_period=time_period)
            title = title or f"RA performance table by groups: {da.get_time_period(prices).to_str()}"
        else:
            prices = self.get_portfolio_nav(time_period=time_period).to_frame()
            title = title or f"RA performance table: {da.get_time_period(prices).to_str()}"
        if benchmark_price is not None:
            if benchmark_price.name not in prices.columns:
                prices = pd.concat([prices, benchmark_price.reindex(index=prices.index, method='ffill')], axis=1)
            ppt.plot_ra_perf_table_benchmark(prices=prices,
                                             benchmark=str(benchmark_price.name),
                                             perf_params=perf_params,
                                             perf_columns=perf_columns,
                                             title=title,
                                             rotation_for_columns_headers=0,
                                             special_rows_colors=[(1, 'deepskyblue'),
                                                                  (len(prices.columns), 'lavender')],
                                             column_header='Portfolio',
                                             ax=ax,
                                             **kwargs)
        else:
            ppt.plot_ra_perf_table(prices=prices,
                                   perf_params=perf_params,
                                   perf_columns=rpt.COMPACT_TABLE_COLUMNS,
                                   title=title,
                                   rotation_for_columns_headers=0,
                                   column_header='Portfolio',
                                   ax=ax,
                                   **kwargs)

    def plot_returns_scatter(self,
                             benchmark_price: pd.Series,
                             is_grouped: bool = True,
                             time_period: da.TimePeriod = None,
                             title: str = None,
                             freq: str = 'QE',
                             ax: plt.Subplot = None,
                             **kwargs
                             ) -> None:
        if is_grouped:
            prices = self.get_group_navs(time_period=time_period)
            title = title or f"Scatterplot of {freq}-returns by groups vs {str(benchmark_price.name)}"
        else:
            prices = self.get_portfolio_nav(time_period=time_period)
            title = title or f"Scatterplot of {freq}-returns vs {str(benchmark_price.name)}"
        prices = pd.concat([prices, benchmark_price.reindex(index=prices.index, method='ffill')], axis=1)
        local_kwargs = sop.update_kwargs(kwargs=kwargs,
                                         new_kwargs={'weight': 'bold',
                                                     'x_rotation': 0,
                                                     'first_color_fixed': False,
                                                     'ci': None})
        prs.plot_returns_scatter(prices=prices,
                                 benchmark=str(benchmark_price.name),
                                 freq=freq,
                                 order=2,
                                 title=title,
                                 ax=ax,
                                 **local_kwargs)

    def plot_monthly_returns_heatmap(self,
                                     time_period: da.TimePeriod = None,
                                     ax: plt.Subplot = None,
                                     **kwargs
                                     ) -> None:
        # for monthly returns fix A and date_format
        kwargs = qis.update_kwargs(kwargs, dict(heatmap_freq='YE', date_format='%Y'))
        plot_returns_heatmap(prices=self.get_portfolio_nav(time_period=time_period),
                             heatmap_column_freq='ME',
                             is_add_annual_column=True,
                             is_inverse_order=True,
                             ax=ax,
                             **kwargs)

    def plot_periodic_returns(self,
                              benchmark_prices: pd.DataFrame = None,
                              is_grouped: bool = True,
                              time_period: da.TimePeriod = None,
                              heatmap_freq: str = 'YE',
                              date_format: str = '%Y',
                              transpose: bool = True,
                              title: str = None,
                              ax: plt.Subplot = None,
                              **kwargs
                              ) -> None:
        if is_grouped:
            prices = self.get_group_navs(time_period=time_period)
            title = title or f"{heatmap_freq}-returns by groups"
        else:
            prices = self.get_portfolio_nav(time_period=time_period).to_frame()
            title = title or f"{heatmap_freq}-returns"
        if benchmark_prices is not None:
            hline_rows = [len(prices.columns)]
            prices = pd.concat([prices, benchmark_prices.reindex(index=prices.index, method='ffill')], axis=1)
        else:
            hline_rows = None

        rhe.plot_periodic_returns_table(prices=prices,
                                        freq=heatmap_freq,
                                        ax=ax,
                                        title=title,
                                        date_format=date_format,
                                        transpose=transpose,
                                        hline_rows=hline_rows,
                                        **kwargs)

    def plot_regime_data(self,
                         benchmark_price: pd.Series,
                         is_grouped: bool = True,
                         regime_data_to_plot: RegimeData = RegimeData.REGIME_SHARPE,
                         time_period: da.TimePeriod = None,
                         var_format: Optional[str] = None,
                         is_conditional_sharpe: bool = True,
                         legend_loc: Optional[str] = 'upper center',
                         title: str = None,
                         perf_params: PerfParams = None,
                         regime_params: BenchmarkReturnsQuantileRegimeSpecs = None,
                         ax: plt.Subplot = None,
                         **kwargs
                         ) -> plt.Figure:

        if is_grouped:
            prices = self.get_group_navs(time_period=time_period)
            title = title or (f"Sharpe ratio attribution by groups to {str(benchmark_price.name)} "
                              f"Bear/Normal/Bull regimes")
        else:
            prices = self.get_portfolio_nav(time_period=time_period).to_frame()
            title = title or f"Sharpe ratio attribution to {str(benchmark_price.name)} Bear/Normal/Bull regimes"

        if benchmark_price.name not in prices.columns:
            prices = pd.concat([benchmark_price.reindex(index=prices.index, method='ffill'), prices], axis=1)

        regime_classifier = rcl.BenchmarkReturnsQuantilesRegime(regime_params=regime_params)
        fig = qis.plot_regime_data(regime_classifier=regime_classifier,
                                   prices=prices,
                                   benchmark=str(benchmark_price.name),
                                   is_conditional_sharpe=is_conditional_sharpe,
                                   regime_data_to_plot=regime_data_to_plot,
                                   var_format=var_format or '{:.2f}',
                                   legend_loc=legend_loc,
                                   perf_params=perf_params,
                                   title=title,
                                   ax=ax,
                                   **kwargs)
        return fig

    def plot_vol_regimes(self,
                         benchmark_price: pd.Series,
                         is_grouped: bool = True,
                         time_period: da.TimePeriod = None,
                         title: str = None,
                         freq: str = 'ME',
                         ax: plt.Subplot = None,
                         **kwargs
                         ) -> plt.Figure:

        if is_grouped:
            prices = self.get_group_navs(time_period=time_period)
            title = title or f"{freq}-returns by groups conditional on vols {str(benchmark_price.name)}"
        else:
            prices = self.get_portfolio_nav(time_period=time_period)
            title = title or f"{freq}-returns conditional on vols {str(benchmark_price.name)}"
        prices = pd.concat([benchmark_price.reindex(index=prices.index, method='ffill'), prices], axis=1)

        regime_classifier = rcl.BenchmarkVolsQuantilesRegime(regime_params=rcl.VolQuantileRegimeSpecs(freq=freq))
        fig = qis.plot_regime_boxplot(regime_classifier=regime_classifier,
                                      prices=prices,
                                      benchmark=str(benchmark_price.name),
                                      title=title,
                                      ax=ax,
                                      **kwargs)
        return fig

    def plot_contributors(self,
                          time_period: da.TimePeriod = None,
                          num_assets: int = 10,
                          ax: plt.Subplot = None,
                          **kwargs
                          ) -> None:
        prices = self.get_instruments_navs(time_period=time_period)
        ppt.plot_top_bottom_performers(prices=prices, num_assets=num_assets, ax=ax, **kwargs)

    def plot_pnl(self, time_period: da.TimePeriod = None) -> None:
        avg_costs, realized_pnl, mtm_pnl, total_pnl, trades = self.compute_realized_pnl(time_period=time_period)
        prices = self.prices
        if time_period is not None:
            prices = time_period.locate(prices)
        with sns.axes_style('darkgrid'):
            fig, axs = plt.subplots(5, 1, figsize=(10, 16), tight_layout=True)
            pts.plot_time_series(df=prices, legend_stats=pts.LegendStats.FIRST_AVG_LAST, title='prices', ax=axs[0])
            pts.plot_time_series(df=avg_costs, legend_stats=pts.LegendStats.FIRST_AVG_LAST, title='avg_costs',
                                 ax=axs[1])
            pts.plot_time_series(df=realized_pnl, legend_stats=pts.LegendStats.FIRST_AVG_LAST, title='realized_pnl',
                                 ax=axs[2])
            pts.plot_time_series(df=mtm_pnl, legend_stats=pts.LegendStats.FIRST_AVG_LAST, title='mtm_pnl', ax=axs[3])
            pts.plot_time_series(df=total_pnl, legend_stats=pts.LegendStats.FIRST_AVG_LAST, title='total_pnl',
                                 ax=axs[4])

    def plot_weights(self,
                     is_input_weights: bool = True,
                     add_mean_levels: bool = False,
                     use_bar_plot: bool = False,
                     columns: List[str] = None,
                     freq: Optional[str] = None,
                     is_yaxis_limit_01: bool = True,
                     bbox_to_anchor: Tuple[float, float] = None,
                     legend_stats: pst.LegendStats = pst.LegendStats.FIRST_AVG_LAST,
                     var_format: str = '{:.1%}',
                     title: Optional[str] = None,
                     ax: plt.Subplot = None,
                     **kwargs
                     ) -> None:
        weights = self.get_weights(is_input_weights=is_input_weights,
                                   columns=columns,
                                   freq=freq)
        pst.plot_stack(df=weights,
                       add_mean_levels=add_mean_levels,
                       use_bar_plot=use_bar_plot,
                       is_yaxis_limit_01=is_yaxis_limit_01,
                       bbox_to_anchor=bbox_to_anchor,
                       title=title,
                       legend_stats=legend_stats,
                       var_format=var_format,
                       ax=ax,
                       **kwargs)

    def plot_performance_attribution(self,
                                     time_period: da.TimePeriod = None,
                                     attribution_metric: AttributionMetric = AttributionMetric.PNL,
                                     ax: plt.Subplot = None,
                                     **kwargs
                                     ) -> None:
        data = self.get_performance_data(attribution_metric=attribution_metric, time_period=time_period)
        kwargs = sop.update_kwargs(kwargs=kwargs,
                                   new_kwargs={'bbox_to_anchor': (0.5, 1.05),
                                               'x_rotation': 90})
        data = data.replace({0.0: np.nan}).dropna()
        qis.plot_bars(df=data,
                      skip_y_axis=True,
                      title=f"{attribution_metric.title}",
                      stacked=False,
                      yvar_format='{:,.2%}',
                      ax=ax,
                      **kwargs)

    def plot_benchmark_betas(self,
                             benchmark_prices: pd.DataFrame,
                             regime_benchmark: str = None,
                             time_period: TimePeriod = None,
                             freq: str = 'W-WED',
                             title: str = None,
                             beta_span: int = 52,
                             regime_params: BenchmarkReturnsQuantileRegimeSpecs = None,
                             add_zero_line: bool = True,
                             ax: plt.Subplot = None,
                             **kwargs
                             ) -> None:
        factor_exposures = self.compute_portfolio_benchmark_betas(benchmark_prices=benchmark_prices,
                                                                  time_period=time_period,
                                                                  freq=freq,
                                                                  span=beta_span)
        qis.plot_time_series(df=factor_exposures,
                             var_format='{:,.2f}',
                             legend_stats=qis.LegendStats.AVG_NONNAN_LAST,
                             title=title or f"Portfolio rolling {beta_span}-span Betas to Benchmarks",
                             ax=ax,
                             **kwargs)

        if regime_benchmark is not None:
            self.add_regime_shadows(ax=ax, regime_benchmark=regime_benchmark, index=factor_exposures.index,
                                    regime_params=regime_params)

        if add_zero_line:
            ax.axhline(0, color='black', lw=1)


@njit
def compute_realized_pnl(prices: np.ndarray,
                         units: np.ndarray
                         ) -> Tuple[np.ndarray, ...]:
    """
    pnl for long only positions, computes avg entry price and pnl by instrument
    """
    avg_costs = np.zeros_like(prices)
    realized_pnl = np.zeros_like(prices)
    mtm_pnl = np.zeros_like(prices)
    trades = np.zeros_like(prices)
    for idx, (price1, unit1) in enumerate(zip(prices, units)):
        if idx == 0:
            avg_costs[idx] = np.where(np.greater(unit1, 1e-16), price1, 0.0)
        else:
            unit0 = units[idx - 1]
            avg_costs0 = avg_costs[idx - 1]
            delta = unit1 - unit0
            is_purchase = np.greater(delta, 1e-16)
            is_sell = np.less(delta, -1e-16)
            realized_pnl[idx] = np.where(is_sell, -delta * (price1 - avg_costs0), 0.0)
            avg_costs[idx] = np.where(is_purchase, np.true_divide(delta * price1 + unit0 * avg_costs0, unit1),
                                      avg_costs0)
            mtm_pnl[idx] = unit0 * (price1 - avg_costs0) - realized_pnl[idx]
            trades[idx] = delta
    return avg_costs, realized_pnl, mtm_pnl, trades


class AllocationType(EnumMap):
    EW = 1
    FIXED_WEIGHTS = 2
    ERC = 3
    ERC_ALT = 4


@dataclass
class PortfolioInput:
    """
    define data inputs for portfolio construction
    """
    name: str
    weights: Union[np.ndarray, pd.DataFrame, Dict[str, float]]
    prices: pd.DataFrame = None  # mandatory but we set none for enumarators
    allocation_type: AllocationType = AllocationType.FIXED_WEIGHTS
    time_period: da.TimePeriod = None
    rebalance_freq: str = 'QE'
    regime_freq: str = 'ME'
    returns_freq: str = 'ME'
    ewm_lambda: float = 0.92
    target_vol: float = None

    def update(self, new: Dict[Any, Any]):
        for key, value in new.items():
            if hasattr(self, key):
                setattr(self, key, value)
