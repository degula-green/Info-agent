"""Keyword-to-URL search used by ``web.research``.

Only the discovery step lives here; reading and extracting pages is the web
package's job. Keeping them apart means a search outage can be reported as a
retryable error without pretending the whole capability is broken.
"""
