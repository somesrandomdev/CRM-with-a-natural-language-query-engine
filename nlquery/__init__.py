"""Natural-language query compiler: question -> IR -> SQL -> validated, read-only execution.

The LLM only appears at the IR boundary (`compiler`) and in the plain-English summary
(`explainer`). Everything between the IR and the database is deterministic code.
"""
