# Local adapter failure diagnostic

Initial symbol-index/Procedrr pass. All twelve Flash candidate-selection and scenario-proposal calls completed; sixteen premise requests failed locally because the old science wrapper placed an extra Context marker ahead of Procedrr's already formatted context. No Typesafe.ai HTTP request was made. Zero added criteria. This is an integration failure, not model-review performance.

The direct production classifier parser fixes the wrapper; the corrected pass at `../symbol-grounded-v2` reuses every completed Flash selection/proposal response. Original failed checkpoints preserved with no overwrite.
