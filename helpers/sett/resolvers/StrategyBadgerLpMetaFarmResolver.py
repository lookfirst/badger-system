from helpers.sett.resolvers.StrategyCoreResolver import StrategyCoreResolver


def confirm_harvest_badger_lp(before, after):
    """
    Harvest Should;
    - Increase the balanceOf() underlying asset in the Strategy
    - Reduce the amount of idle BADGER to zero
    - Increase the ppfs on sett
    """

    assert after.strategy.balanceOf >= before.strategy.balanceOf
    if before.sett.pricePerFullShare:
        assert after.sett.pricePerFullShare > before.sett.pricePerFullShare


class StrategyBadgerLpMetaFarmResolver(StrategyCoreResolver):
    def confirm_harvest(self, before, after, tx):
        super().confirm_harvest(before, after, tx)
        # Strategy want should increase
        before_balance = before.get("strategy.balanceOf")
        assert after.get("strategy.balanceOf") >= before_balance if before_balance else 0

        # PPFS should not decrease
        assert after.get("sett.pricePerFullShare") >= before.get("sett.pricePerFullShare")

    def get_strategy_destinations(self):
        strategy = self.manager.strategy
        return {"stakingRewards": strategy.geyser()}
