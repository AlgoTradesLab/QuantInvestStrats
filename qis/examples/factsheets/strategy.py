import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from typing import Tuple, List
from enum import Enum
import yfinance as yf
import qis
from qis import TimePeriod, PortfolioData


from qis.portfolio.reports.config import KWARG_LONG, KWARG_SHORT, PERF_PARAMS, REGIME_PARAMS


def fetch_riskparity_universe_data() -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """
    define custom universe with asset class grouping
    """
    universe_data = dict(SPY='Equities',
                         QQQ='Equities',
                         EEM='Equities',
                         TLT='Bonds',
                         IEF='Bonds',
                         SHY='Bonds',
                         LQD='Credit',
                         HYG='HighYield',
                         GLD='Gold')
    tickers = list(universe_data.keys())
    group_data = pd.Series(universe_data)  # for portfolio reporting
    prices = yf.download(tickers=tickers, start=None, end=None, ignore_tz=True)['Adj Close'][tickers]
    benchmark_prices = prices[['SPY', 'TLT']]
    return prices, benchmark_prices, group_data


def fetch_equity_bond() -> Tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """
    define custom universe with asset class grouping
    """
    universe_data = dict(SPY='Equities',
                         IEF='Bonds')
    tickers = list(universe_data.keys())
    group_data = pd.Series(universe_data)  # for portfolio reporting
    prices = yf.download(tickers=tickers, start=None, end=None, ignore_tz=True)['Adj Close'][tickers]
    benchmark_prices = prices[['SPY', 'IEF']]
    return prices, benchmark_prices, group_data


def generate_volparity_portfolio(prices: pd.DataFrame,
                                 group_data: pd.Series,
                                 time_period: TimePeriod = None,
                                 span: int = 60,
                                 vol_target: float = 0.15
                                 ) -> PortfolioData:
    ra_returns, weights, ewm_vol = qis.compute_ra_returns(returns=qis.to_returns(prices=prices, is_log_returns=True),
                                                          span=span,
                                                          vol_target=vol_target)
    weights = weights.divide(weights.sum(1), axis=0)

    if time_period is not None:
        weights = time_period.locate(weights)

    volparity_portfolio = qis.backtest_model_portfolio(prices=prices,
                                                       weights=weights,
                                                       is_output_portfolio_data=True,
                                                       ticker='VolParity')
    volparity_portfolio._set_group_data(group_data=group_data, group_order=list(group_data.unique()))
    return volparity_portfolio


def generate_equity_bond_portfolio(prices: pd.DataFrame,
                                   weights: List[float],
                                   group_data: pd.Series
                                   ) -> PortfolioData:
    volparity_portfolio = qis.backtest_model_portfolio(prices=prices,
                                                       weights=weights,
                                                       is_output_portfolio_data=True,
                                                       ticker='EquityBond')
    volparity_portfolio._set_group_data(group_data=group_data, group_order=list(group_data.unique()))
    return volparity_portfolio


class UnitTests(Enum):
    VOLPARITY_PORTFOLIO = 1
    EQUITY_BOND = 2


def run_unit_test(unit_test: UnitTests):

    time_period = qis.TimePeriod('31Dec2005', '06Jul2023')  # time period for portfolio reporting

    if unit_test == UnitTests.VOLPARITY_PORTFOLIO:
        prices, benchmark_prices, group_data = fetch_riskparity_universe_data()
        portfolio_data = generate_volparity_portfolio(prices=prices,
                                                      group_data=group_data,
                                                      time_period=time_period,
                                                      span=30,
                                                      vol_target=0.15)
        fig = qis.generate_strategy_factsheet(portfolio_data=portfolio_data,
                                              benchmark_prices=benchmark_prices,
                                              time_period=time_period,
                                              **KWARG_LONG)
        qis.save_figs_to_pdf(figs=[fig],
                             file_name=f"{portfolio_data.nav.name}_portfolio_factsheet_long",
                             orientation='landscape',
                             local_path=qis.local_path.get_output_path())

        fig = qis.generate_strategy_factsheet(portfolio_data=portfolio_data,
                                              benchmark_prices=benchmark_prices,
                                              time_period=TimePeriod('31Dec2019', time_period.end),
                                              **KWARG_SHORT)
        qis.save_figs_to_pdf(figs=[fig],
                             file_name=f"{portfolio_data.nav.name}_portfolio_factsheet_short",
                             orientation='landscape',
                             local_path=qis.local_path.get_output_path())

        qis.save_fig(fig=fig, file_name=f"strategy", local_path=qis.local_path.get_output_path())

    elif unit_test == UnitTests.EQUITY_BOND:
        prices, benchmark_prices, group_data = fetch_equity_bond()
        portfolio_data = generate_equity_bond_portfolio(prices=prices,
                                                        weights=[0.6, 0.4],
                                                        group_data=group_data)
        fig = qis.generate_strategy_factsheet(portfolio_data=portfolio_data,
                                              benchmark_prices=benchmark_prices,
                                              time_period=time_period,
                                              **KWARG_LONG)
        qis.save_figs_to_pdf(figs=[fig],
                             file_name=f"{portfolio_data.nav.name}_portfolio_factsheet_long",
                             orientation='landscape',
                             local_path=qis.local_path.get_output_path())

        fig = qis.generate_strategy_factsheet(portfolio_data=portfolio_data,
                                              benchmark_prices=benchmark_prices,
                                              time_period=TimePeriod('31Dec2019', time_period.end),
                                              **KWARG_SHORT)
        qis.save_figs_to_pdf(figs=[fig],
                             file_name=f"{portfolio_data.nav.name}_portfolio_factsheet_short",
                             orientation='landscape',
                             local_path=qis.local_path.get_output_path())

    # plt.show()


if __name__ == '__main__':

    unit_test = UnitTests.VOLPARITY_PORTFOLIO

    is_run_all_tests = False
    if is_run_all_tests:
        for unit_test in UnitTests:
            run_unit_test(unit_test=unit_test)
    else:
        run_unit_test(unit_test=unit_test)