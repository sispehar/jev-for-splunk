"""jev_core: the splunklib-free heart of the jev_for_splunk Splunk app.

Everything here runs on Splunk's bundled Python (3.9 and 3.13) with the
standard library only. The Splunk command wrappers in bin/ are thin; this
package holds the logic so it can be unit tested and driven from the command
line (python -m jev_core.cli).
"""
from __future__ import annotations

__version__ = "0.1.0"
USER_AGENT = "jev_for_splunk/" + __version__
