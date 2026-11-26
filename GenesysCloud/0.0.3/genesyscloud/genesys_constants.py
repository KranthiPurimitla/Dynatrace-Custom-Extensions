"""
Central constants for the Genesys Cloud extension.

Only Phase 0 (Platform Health) and Phase 1 (Conversations) are defined here.
Later phases add their keys to this file when implemented, so collectors and
extension.yaml always reference one source of truth.
"""

# ---------------------------------------------------------------------------
# Genesys Cloud regions: each maps to a login host (OAuth) and an API host.
# https://developer.genesys.cloud/platform/api/
# ---------------------------------------------------------------------------
REGIONS = {
    "us-east-1": {"login": "login.mypurecloud.com", "api": "api.mypurecloud.com"},
    "us-west-2": {"login": "login.usw2.pure.cloud", "api": "api.usw2.pure.cloud"},
    "ca-central-1": {"login": "login.cac1.pure.cloud", "api": "api.cac1.pure.cloud"},
    "eu-west-1": {"login": "login.mypurecloud.ie", "api": "api.mypurecloud.ie"},
    "eu-west-2": {"login": "login.euw2.pure.cloud", "api": "api.euw2.pure.cloud"},
    "eu-central-1": {"login": "login.mypurecloud.de", "api": "api.mypurecloud.de"},
    "eu-central-2": {"login": "login.euc2.pure.cloud", "api": "api.euc2.pure.cloud"},
    "ap-southeast-2": {"login": "login.mypurecloud.com.au", "api": "api.mypurecloud.com.au"},
    "ap-northeast-1": {"login": "login.mypurecloud.jp", "api": "api.mypurecloud.jp"},
    "ap-south-1": {"login": "login.aps1.pure.cloud", "api": "api.aps1.pure.cloud"},
}

# ---------------------------------------------------------------------------
# API paths (Aggregates APIs only - per Requirement 7)
# ---------------------------------------------------------------------------
TOKEN_PATH = "/oauth/token"  # noqa: S105 - this is a URL path, not a secret
PATH_CONVERSATION_AGGREGATES = "/api/v2/analytics/conversations/aggregates/query"
PATH_QUEUE_AGGREGATES = "/api/v2/analytics/queues/aggregates/query"  # reserved for Phase 2

# ---------------------------------------------------------------------------
# Feature sets (one per phase; must match extension.yaml featureSets)
# ---------------------------------------------------------------------------
FS_PLATFORM = "platform_health"  # Phase 0
FS_CONVERSATIONS = "conversations"  # Phase 1

# ---------------------------------------------------------------------------
# Phase 0 - Platform Health metric keys
# ---------------------------------------------------------------------------
M_API_RESPONSE_TIME = "genesys.api.response.time"
M_API_CALLS = "genesys.api.calls"
M_API_ERRORS = "genesys.api.errors"
M_API_AUTH_FAILURES = "genesys.api.auth.failures"
M_API_RATE_LIMIT = "genesys.api.rate.limit"

# ---------------------------------------------------------------------------
# Phase 1 - Conversations metric keys
# ---------------------------------------------------------------------------
M_CONV_COUNT = "genesys.conversation.count"
M_CONV_INBOUND = "genesys.conversation.inbound"
M_CONV_OUTBOUND = "genesys.conversation.outbound"
M_CONV_HANDLE = "genesys.conversation.handle.time"
M_CONV_TALK = "genesys.conversation.talk.time"
M_CONV_WAIT = "genesys.conversation.wait.time"
M_CONV_HOLD = "genesys.conversation.hold.time"
M_CONV_ACW = "genesys.conversation.acw.time"

# ---------------------------------------------------------------------------
# Genesys analytics aggregate field names requested for conversations.
# Duration fields are returned in milliseconds; helpers convert to seconds.
#   tHandle  -> handle time      tTalk -> talk time
#   tHeld    -> hold time        tAcw  -> after-call work
#   tWait    -> ACD wait time    nOffered -> offered count
# ---------------------------------------------------------------------------
GENESYS_CONV_METRICS = ["nOffered", "tHandle", "tTalk", "tHeld", "tAcw", "tWait"]

# Minutes to shift the query window into the past, to absorb Genesys's
# conversation-aggregate finalization lag (the most recent minute is often
# not yet queryable).
CONV_AGGREGATE_LAG_MINUTES = 5

# Width (in minutes) of the query window itself. Wider than the 5-minute
# collection cadence on purpose: ConversationsCollector requests per-minute
# granularity within this window and tracks the latest bucket end-time it has
# already reported, so overlapping queries across cycles never double-count
# DELTA metrics. Set to 60 minutes to reliably capture conversations in
# low-volume orgs (sparse traffic means short windows frequently return empty).
CONV_QUERY_WINDOW_MINUTES = 60

# Duration field -> our metric key (average over the interval, in seconds)
CONV_DURATION_MAP = {
    "tHandle": M_CONV_HANDLE,
    "tTalk": M_CONV_TALK,
    "tHeld": M_CONV_HOLD,
    "tAcw": M_CONV_ACW,
    "tWait": M_CONV_WAIT,
}

# ---------------------------------------------------------------------------
# Allowed low-cardinality dimension keys (per Requirement 8)
# ---------------------------------------------------------------------------
DIM_DIRECTION = "direction"
DIM_MEDIA_TYPE = "media_type"
DIM_QUEUE_NAME = "queue_name"
DIM_DIVISION = "division_name"
DIM_ORG_NAME = "org_name"      # cardinality 1; keys the genesys:organization entity (health metrics)
DIM_CONV_NAME = "conversation_name"  # synthetic "{direction}_{media_type}"; keys the genesys:conversation entity

# ---------------------------------------------------------------------------
# Region → approximate UTC offset (hours, standard time).
# Used only for business-hours log hints — DST precision is not required.
# ---------------------------------------------------------------------------
REGION_UTC_OFFSET_HOURS: dict[str, float] = {
    "us-east-1":      -5.0,   # US Eastern Standard
    "us-west-2":      -8.0,   # US Pacific Standard
    "ca-central-1":   -5.0,   # Canada Eastern Standard
    "eu-west-1":       0.0,   # Ireland / UTC
    "eu-west-2":       0.0,   # UK
    "eu-central-1":    1.0,   # Central European Standard
    "eu-central-2":    1.0,   # Central European Standard
    "ap-southeast-2": 10.0,   # Australian Eastern Standard
    "ap-northeast-1":  9.0,   # Japan Standard
    "ap-south-1":      5.5,   # India Standard Time
}

# Typical contact-centre business hours in local region time (inclusive start,
# exclusive end). Used only for log hints — not enforced on collection.
BUSINESS_HOURS_START = 8   # 08:00 local
BUSINESS_HOURS_END   = 18  # 18:00 local

# ---------------------------------------------------------------------------
# HTTP behaviour
# ---------------------------------------------------------------------------
DEFAULT_TIMEOUT_SECONDS = 30
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 1.5
TOKEN_REFRESH_SKEW_SECONDS = 60
DEFAULT_PAGE_SIZE = 100
