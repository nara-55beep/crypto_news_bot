"""Lucid Strategy Lab: rule-aware research and simulation support.

This package deliberately contains no broker or order-routing capability.
"""

from .rules import RULES_LAST_CHECKED, get_account_rules, public_evaluation_options

__all__ = ["RULES_LAST_CHECKED", "get_account_rules", "public_evaluation_options"]
