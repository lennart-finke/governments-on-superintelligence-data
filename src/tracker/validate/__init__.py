"""Hand-validation of judge labels: stratified sampler, browser reviewer, report.

A model judge is checked against a human here, selected by `adjudications.role`:
'primary' is the bulk judge every candidate gets, 'confirm' the second pass that
is no longer in the default pipeline but whose verdicts are still in the log.
Sampling is label-balanced, English-only and
stratified by jurisdiction and year (see sample.py); web.py serves a local
keyboard-driven reviewer that records one human judgement per item into
`validation_labels`; report.py turns those into agreement rates and
stratum-reweighted corpus estimates with Wilson CIs, plus Cohen's kappa
(kappa.py) for the chance-corrected view of the same labels.

Review is blind, and blindness is a property of the server rather than of the
page: web.py never puts the judge's verdict on the wire for an item the
reviewer has not yet committed to.
"""

from .report import agreement_report
from .sample import JUDGES, LANG, build_sample, load_sample, reset_tables

__all__ = ["JUDGES", "LANG", "agreement_report", "build_sample", "load_sample", "reset_tables"]
