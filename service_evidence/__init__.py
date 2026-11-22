"""居家服务组合证据与目录换版分析工具。"""

from .data import load_tables
from .aliases import AliasResolver, Resolution
from .catalog import Catalog, CombinationCheck
from .baskets import Observation, build_observations, dedupe_by_household
from .evidence import PairEvidence, analyze_pairs, pending_mapping_impact
from .advice import AdviceReview, alias_impact, review_sent_advice, current_guidance
from .counselor import CounselorView, counselor_view
from .report import build_report

__all__ = [
    "load_tables",
    "AliasResolver",
    "Resolution",
    "Catalog",
    "CombinationCheck",
    "Observation",
    "build_observations",
    "dedupe_by_household",
    "PairEvidence",
    "analyze_pairs",
    "pending_mapping_impact",
    "AdviceReview",
    "alias_impact",
    "review_sent_advice",
    "current_guidance",
    "CounselorView",
    "counselor_view",
    "build_report",
]
