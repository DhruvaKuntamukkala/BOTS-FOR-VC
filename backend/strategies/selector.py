from enum import Enum
from urllib.parse import urlparse


class Platform(Enum):
    ZOOM  = "zoom"
    MEET  = "meet"
    WEBEX = "webex"
    TEAMS = "teams"
    ZOHO  = "zoho"
    UNKNOWN = "unknown"


def detect_platform(url: str) -> Platform:
    netloc = urlparse(url).netloc.lower()
    if 'zoom.us' in netloc:
        return Platform.ZOOM
    if 'meet.google.com' in netloc:
        return Platform.MEET
    if 'webex.com' in netloc:
        return Platform.WEBEX
    if 'teams.microsoft.com' in netloc or 'teams.live.com' in netloc:
        return Platform.TEAMS
    if 'zoho.com' in netloc or 'zohomeeting.com' in netloc or 'zoho.in' in netloc:
        return Platform.ZOHO
    return Platform.UNKNOWN


class StrategySelector:
    @staticmethod
    def get_strategy(platform: Platform):
        """
        Returns (BotClass, use_rtms).

        All bots (Zoom, Teams, Webex, Meet, Zoho) now use Playwright
        browser automation as the primary strategy.
        """
        from backend.bots.zoom_sdk_bot  import ZoomSDKBot
        from backend.bots.teams_sdk_bot import TeamsSDKBot
        from backend.bots.webex_sdk_bot import WebexSDKBot
        from backend.bots.meet_bot      import MeetBot
        from backend.bots.zoho_bot      import ZohoBot

        strategies = {
            Platform.ZOOM:  (ZoomSDKBot,  False),
            Platform.MEET:  (MeetBot,     False),
            Platform.WEBEX: (WebexSDKBot, False),
            Platform.TEAMS: (TeamsSDKBot, False),
            Platform.ZOHO:  (ZohoBot,     False),
        }

        return strategies.get(platform, (None, False))
