import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from functools import partial, wraps
from itertools import zip_longest
from os import times
from pathlib import Path

import toml
from brownie import MerkleDistributor, Wei, accounts, interface, rpc, web3
from brownie.utils import color
from click import secho
from dotmap import DotMap
from eth_abi import decode_single, encode_single
from eth_abi.packed import encode_abi_packed
from eth_utils import encode_hex
from toolz.itertoolz import last
from helpers.constants import AddressZero
from rich.console import Console
from toolz import valfilter, valmap
from tqdm import tqdm, trange
from tabulate import tabulate

console = Console()


def val(amount):
    return "{:,.6f}".format(amount / 1e18)


def sec(amount):
    return "{:,.1f}".format(amount / 1e12)


class BadgerGeyserMock:
    def __init__(self, key):
        self.key = key
        self.events = DotMap()
        self.stakes = DotMap()
        self.totalShareSeconds = 0
        self.users = DotMap()
        self.unlockSchedules = DotMap()
        self.distributionTokens = []
        self.totalDistributions = DotMap()
        self.totalShareSecondsInRange = 0

    # ===== Setters =====

    def set_current_period(self, startTime, endTime):
        self.startTime = startTime
        self.endTime = endTime

    def set_stakes():
        """
        Set the current stakes for a user based on historical events
        """
        return False

    def add_distribution_token(self, token):
        self.distributionTokens.append(token)

    def add_unlock_schedule(self, token, unlockSchedule):
        if not self.unlockSchedules[str(token)]:
            self.unlockSchedules[str(token)] = []

        parsedSchedule = DotMap(
            initialTokensLocked=unlockSchedule[0],
            endTime=unlockSchedule[1],
            duration=unlockSchedule[2],
            startTime=unlockSchedule[3],
        )

        self.unlockSchedules[str(token)].append(parsedSchedule)

        console.log(
            "add_unlock_schedule for", str(token), parsedSchedule.toDict(),
        )

    def get_distributed_for_token_at(self, token, endTime):
        """
        Get total distribution for token within range, across unlock schedules
        """
        totalToDistribute = 0

        unlockSchedules = self.unlockSchedules[token]
        for schedule in unlockSchedules:
            rangeDuration = endTime - schedule.startTime
            toDistribute = min(
                schedule.initialTokensLocked,
                int(schedule.initialTokensLocked * rangeDuration // schedule.duration),
            )
            # TODO: May need to add af few % here

            totalToDistribute += toDistribute
        return totalToDistribute

    def calc_token_distributions_in_range(self, startTime, endTime):
        tokenDistributions = DotMap()
        for token in self.distributionTokens:
            tokenDistributions[token] = int((self.get_distributed_for_token_at(
                token, endTime
            ) - self.get_distributed_for_token_at(token, startTime)))
            self.totalDistributions[token] = tokenDistributions[token]

        return tokenDistributions

    def calc_token_distributions_at_time(self, endTime):
        """
        For each distribution token tracked by this Geyser, determine how many tokens should be distributed in the specified range.
        This is found by summing the values from all unlockSchedules during the range for this token
        """
        tokenDistributions = DotMap()
        console.print("[cyan]== Calculate Token Distributions ==[/cyan]")
        # console.log(
        #     {
        #         "startTime": startTime,
        #         "endTime": endTime,
        #         "tokens": self.distributionTokens,
        #     }
        # )
        for token in self.distributionTokens:
            tokenDistributions[token] = self.get_distributed_for_token_at(
                token, endTime
            )
            self.totalDistributions[token] = tokenDistributions[token]

        return tokenDistributions

    def get_token_totals_from_user_dists(self, userDistributions):
        tokenTotals = {}
        for user, userData in userDistributions.items():
            for token, tokenAmount in userData.items():
                if token in tokenTotals:
                    tokenTotals[token] += tokenAmount
                else:
                    tokenTotals[token] = tokenAmount
        return tokenTotals

    def calc_user_distributions(self, tokenDistributions):
        userDistributions = {}
        userMetadata = {}
        """
        Each user should get their proportional share of each token
        """
        totalShareSecondsUsed = 0
        # console.log("tokenDistributions", tokenDistributions.toDict())

        for user, userData in self.users.items():
            userDistributions[user] = {}
            userMetadata[user] = {}
            for token, tokenAmount in tokenDistributions.items():
                # Record total share seconds
                if not "shareSeconds" in userData:
                    userMetadata[user]["shareSeconds"] = 0
                else:
                    userMetadata[user]["shareSeconds"] = userData.shareSeconds

                # Track Distribution based on seconds in range
                if "shareSecondsInRange" in userData:
                    userMetadata[user][
                        "shareSecondsInRange"
                    ] = userData.shareSecondsInRange
                    totalShareSecondsUsed += userData.shareSecondsInRange
                    userShare = int(
                        tokenAmount
                        * userData.shareSecondsInRange
                        // self.totalShareSecondsInRange
                    )
                    userDistributions[user][token] = userShare

                else:
                    userDistributions[user][token] = 0
                    userMetadata[user]["shareSecondsInRange"] = 0

        assert totalShareSecondsUsed == self.totalShareSecondsInRange
        tokenTotals = self.get_token_totals_from_user_dists(userDistributions)
        # self.printState()

        # Check values vs total for each token
        for token, totalAmount in tokenTotals.items():
            # NOTE The total distributed should be less than or equal to the actual tokens distributed. Rounding dust will go to DAO
            # NOTE The value of the distributed should only be off by a rounding error
            print("duration ", (self.endTime - self.startTime) / 3600)
            print("totalAmount ", totalAmount / 1e18)
            print("self.totalDistributions ", self.totalDistributions[token] / 1e18)
            print("totalAmount ", totalAmount)
            print("self.totalDistributions ", self.totalDistributions[token])
            print("leftover", abs(self.totalDistributions[token] - totalAmount))
            assert totalAmount <= self.totalDistributions[token]
            assert abs(self.totalDistributions[token] - totalAmount) < 30000

        return {
            "claims": userDistributions,
            "totals": tokenTotals,
            "metadata": userMetadata,
        }

    def unstake(self, user, unstake):
        # Update share seconds on unstake
        self.process_share_seconds(user, unstake.timestamp)

        # Process unstakes from individual stakes
        toUnstake = int(unstake.amount)
        while toUnstake > 0:
            stake = self.users[user].stakes[-1]

            # This stake won't cover, remove
            if toUnstake >= stake["amount"]:
                self.users[user].stakes.pop()
                toUnstake -= stake["amount"]

            # This stake will cover the unstaked amount, reduce
            else:
                self.users[user].stakes[-1]["amount"] -= toUnstake
                toUnstake = 0

        # Update globals
        self.users[user].total = unstake.userTotal
        self.users[user].lastUpdate = unstake.timestamp

        # console.log("unstake", self.users[user].toDict(), unstake, self.users[user])

    def stake(self, user, stake):
        # Update share seconds for previous stakes on stake
        self.process_share_seconds(user, stake.timestamp)

        # Add Stake
        self.addStake(user, stake)

        # Update Globals
        self.users[user].lastUpdate = stake.timestamp
        self.users[user].total = stake.userTotal

    def addStake(self, user, stake):
        if not self.users[user].stakes:
            self.users[user].stakes = []
        self.users[user].stakes.append(
            {"amount": stake.amount, "stakedAt": stake.stakedAt}
        )

    def calc_end_share_seconds_for(self, user):
        self.process_share_seconds(user, self.endTime)
        self.users[user].lastUpdate = self.endTime

    def calc_end_share_seconds(self):
        """
        Process share seconds after the last action of each user, up to the end time
        If the user took no actions during the claim period, calculate their shareSeconds from their pre-existing stakes
        """

        for user in self.users:
            self.process_share_seconds(user, self.endTime)

    def process_share_seconds(self, user, timestamp):
        data = self.users[user]

        # Return 0 if user has no tokens
        if not "total" in data:
            return 0

        lastUpdate = self.getLastUpdate(user)

        # Either cycle start or last update, whichever comes later
        lastUpdateRangeGated = max(self.startTime, int(lastUpdate))

        timeSinceLastAction = int(timestamp) - int(lastUpdate)
        timeSinceLastActionRangeGated = int(timestamp) - int(lastUpdateRangeGated)

        if timeSinceLastAction == 0:
            return 0

        toAdd = 0
        toAddInRange = 0

        for stake in data.stakes:
            toAdd += stake["amount"] * int(timeSinceLastAction)
            if timestamp > self.startTime:
                toAddInRange += stake["amount"] * int(timeSinceLastActionRangeGated)
        assert toAdd >= 0

        # If user has share seconds, add
        if "shareSeconds" in data:
            data.shareSeconds += toAdd
            self.totalShareSeconds += toAdd

        # If user has no share seconds, set
        else:
            data.shareSeconds = toAdd
            self.totalShareSeconds += toAdd

        if "shareSecondsInRange" in data:
            data.shareSecondsInRange += toAddInRange
        else:
            data.shareSecondsInRange = toAddInRange
        self.totalShareSecondsInRange += toAddInRange

        self.users[user] = data

    # ===== Getters =====

    def getLastUpdate(self, user):
        """
        Get the last time the specified user took an action
        """
        if not self.users[user].lastUpdate:
            return 0
        return self.users[user].lastUpdate

    def printState(self):
        table = []
        # console.log("User State", self.users.toDict(), self.totalShareSeconds)
        for user, data in self.users.items():

            rewards = self.userDistributions["claims"][user]["0x3472A5A71965499acd81997a54BBA8D852C6E53d"]
            data.shareSecondsInRange

            sharesPerReward = 0
            if rewards > 0:
                sharesPerReward = data.shareSecondsInRange / rewards

            table.append(
                [
                    user,
                    val(rewards),
                    sec(data.shareSecondsInRange),
                    sharesPerReward,
                    sec(data.shareSeconds),
                    data.total,
                    data.lastUpdate,
                ]
            )
        print("GEYSER " + self.key)
        print(
            tabulate(
                table,
                headers=[
                    "user",
                    "rewards",
                    "shareSecondsInRange",
                    "shareSeconds/reward",
                    "shareSeconds",
                    "totalStaked",
                    "lastUpdate",
                ],
            )
        )
        print(self.userDistributions["totals"]['0x3472A5A71965499acd81997a54BBA8D852C6E53d'] / 1e18)

        # console.log('printState')