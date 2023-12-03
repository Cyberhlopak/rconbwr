import logging
import re
from datetime import datetime
from functools import wraps
from threading import Timer

from discord_webhook import DiscordEmbed

from rcon.cache_utils import invalidates
from rcon.commands import CommandFailedError, HLLServerError
from rcon.discord import (
    dict_to_discord,
    get_prepared_discord_hooks,
    send_to_discord_audit,
)
from rcon.game_logs import (
    on_camera,
    on_chat,
    on_connected,
    on_disconnected,
    on_match_end,
    on_match_start,
)
from rcon.models import enter_session
from rcon.player_history import (
    _get_set_player,
    get_player,
    safe_save_player_action,
    save_end_player_session,
    save_player,
    save_start_player_session,
)
from rcon.rcon import Rcon, StructuredLogLineType
from rcon.steam_utils import get_player_bans, get_steam_profile, update_db_player_info
from rcon.types import PlayerFlagType, SteamBansType
from rcon.user_config.auto_mod_no_leader import AutoModNoLeaderUserConfig
from rcon.user_config.camera_notification import CameraNotificationUserConfig
from rcon.user_config.real_vip import RealVipUserConfig
from rcon.user_config.vac_game_bans import VacGameBansUserConfig
from rcon.user_config.webhooks import CameraWebhooksUserConfig
from rcon.user_config.message_on_connect import MessageOnConnectUserConfig
from rcon.utils import LOG_MAP_NAMES_TO_MAP, UNKNOWN_MAP_NAME, MapsHistory
from rcon.vote_map import VoteMap
from rcon.workers import record_stats_worker, temporary_broadcast, temporary_welcome

logger = logging.getLogger(__name__)


@on_chat
def count_vote(rcon: Rcon, struct_log: StructuredLogLineType):
    enabled = VoteMap().handle_vote_command(rcon=rcon, struct_log=struct_log)
    if enabled and (match := re.match(r"\d\s*$", struct_log["sub_content"].strip())):
        rcon.do_message_player(
            steam_id_64=struct_log["steam_id_64_1"],
            message=f"INVALID VOTE\n\nUse: !votemap {match.group()}",
        )


def initialise_vote_map(rcon: Rcon, struct_log):
    logger.info("New match started initializing vote map. %s", struct_log)
    try:
        vote_map = VoteMap()
        vote_map.clear_votes()
        vote_map.gen_selection()
        vote_map.reset_last_reminder_time()
        vote_map.apply_results()
    except:
        logger.exception("Something went wrong in vote map init")


@on_match_end
def remind_vote_map(rcon: Rcon, struct_log):
    logger.info("Match ended reminding to vote map. %s", struct_log)
    vote_map = VoteMap()
    vote_map.apply_with_retry()
    vote_map.vote_map_reminder(rcon, force=True)


@on_match_start
def handle_new_match_start(rcon: Rcon, struct_log):
    try:
        logger.info("New match started recording map %s", struct_log)
        with invalidates(Rcon.get_map):
            try:
                current_map = rcon.get_map().replace("_RESTART", "")
            except (CommandFailedError, HLLServerError):
                current_map = "bla_"
                logger.error("Unable to get current map")

        map_name_to_save = LOG_MAP_NAMES_TO_MAP.get(
            struct_log["sub_content"], UNKNOWN_MAP_NAME
        )
        guessed = True
        log_map_name = struct_log["sub_content"].rsplit(" ")[0]
        log_time = datetime.fromtimestamp(struct_log["timestamp_ms"] / 1000)
        # Check that the log is less than 5min old
        if (datetime.utcnow() - log_time).total_seconds() < 5 * 60:
            # then we use the current map to be more accurate
            if (
                current_map.split("_")[0].lower()
                == map_name_to_save.split("_")[0].lower()
            ):
                map_name_to_save = current_map
                guessed = False
            elif map_name_to_save == UNKNOWN_MAP_NAME:
                map_name_to_save = current_map
                guessed = True
            else:
                logger.warning(
                    "Got recent match start but map don't match %s != %s",
                    map_name_to_save,
                    current_map,
                )

        # TODO added guess - check if it's already in there - set prev end if None
        maps_history = MapsHistory()

        if len(maps_history) > 0:
            if maps_history[0]["end"] is None and maps_history[0]["name"]:
                maps_history.save_map_end(
                    old_map=maps_history[0]["name"],
                    end_timestamp=int(struct_log["timestamp_ms"] / 1000) - 100,
                )

        maps_history.save_new_map(
            new_map=map_name_to_save,
            guessed=guessed,
            start_timestamp=int(struct_log["timestamp_ms"] / 1000),
        )
    except:
        raise
    finally:
        initialise_vote_map(rcon, struct_log)
        try:
            record_stats_worker(MapsHistory()[1])
        except Exception:
            logger.exception("Unexpected error while running stats worker")


@on_match_end
def record_map_end(rcon: Rcon, struct_log):
    logger.info("Match ended recording map %s", struct_log)
    maps_history = MapsHistory()
    try:
        current_map = rcon.get_map()
    except (CommandFailedError, HLLServerError):
        current_map = "bla_"
        logger.error("Unable to get current map")

    map_name = LOG_MAP_NAMES_TO_MAP.get(struct_log["sub_content"], UNKNOWN_MAP_NAME)
    log_time = datetime.fromtimestamp(struct_log["timestamp_ms"] / 1000)

    if (datetime.utcnow() - log_time).total_seconds() < 60:
        # then we use the current map to be more accurate
        if current_map.split("_")[0].lower() == map_name.split("_")[0].lower():
            maps_history.save_map_end(
                current_map, end_timestamp=int(struct_log["timestamp_ms"] / 1000)
            )


def ban_if_blacklisted(rcon: Rcon, steam_id_64, name):
    with enter_session() as sess:
        player = get_player(sess, steam_id_64)

        if not player:
            logger.error("Can't check blacklist, player not found %s", steam_id_64)
            return

        if player.blacklist and player.blacklist.is_blacklisted:
            try:
                logger.info(
                    "Player %s was banned due blacklist, reason: %s",
                    str(name),
                    player.blacklist.reason,
                )
                rcon.do_perma_ban(
                    player=name,
                    reason=player.blacklist.reason,
                    by=f"BLACKLIST: {player.blacklist.by}",
                )
                safe_save_player_action(
                    rcon=rcon,
                    player_name=name,
                    action_type="PERMABAN",
                    reason=player.blacklist.reason,
                    by=f"BLACKLIST: {player.blacklist.by}",
                    steam_id_64=steam_id_64,
                )
                try:
                    send_to_discord_audit(
                        f"`BLACKLIST` -> {dict_to_discord(dict(player=name, reason=player.blacklist.reason))}",
                        "BLACKLIST",
                    )
                except:
                    logger.error("Unable to send blacklist to audit log")
            except:
                send_to_discord_audit(
                    "Failed to apply ban on blacklisted players, please check the logs and report the error",
                    "ERROR",
                )


def should_ban(
    bans: SteamBansType | None,
    max_game_bans: float,
    max_days_since_ban: int,
    player_flags: list[PlayerFlagType] = [],
    whitelist_flags: list[str] = [],
) -> bool | None:
    if not bans:
        return

    if any(player_flag in whitelist_flags for player_flag in player_flags):
        return False

    try:
        days_since_last_ban = int(bans["DaysSinceLastBan"])
        number_of_game_bans = int(bans.get("NumberOfGameBans", 0))
    except ValueError:  # In case DaysSinceLastBan can be null
        return

    has_a_ban = bans.get("VACBanned") == True or number_of_game_bans >= max_game_bans

    if days_since_last_ban <= 0:
        return False

    if days_since_last_ban <= max_days_since_ban and has_a_ban:
        return True

    return False


def ban_if_has_vac_bans(rcon: Rcon, steam_id_64, name):
    config = VacGameBansUserConfig.load_from_db()

    max_days_since_ban = config.vac_history_days
    max_game_bans = (
        float("inf") if config.game_ban_threshhold <= 0 else config.game_ban_threshhold
    )
    whitelist_flags = config.whitelist_flags

    if max_days_since_ban <= 0:
        return  # Feature is disabled

    with enter_session() as sess:
        player = get_player(sess, steam_id_64)

        if not player:
            logger.error("Can't check VAC history, player not found %s", steam_id_64)
            return

        bans: SteamBansType | None = get_player_bans(steam_id_64)
        if not bans or not isinstance(bans, dict):
            logger.warning(
                "Can't fetch Bans for player %s, received %s", steam_id_64, bans
            )
            # Player couldn't be fetched properly (logged by get_player_bans)
            return

        if should_ban(
            bans,
            max_game_bans,
            max_days_since_ban,
            player_flags=player.flags,
            whitelist_flags=whitelist_flags,
        ):
            reason = config.ban_on_vac_history_reason.format(
                DAYS_SINCE_LAST_BAN=bans.get("DaysSinceLastBan"),
                MAX_DAYS_SINCE_BAN=str(max_days_since_ban),
            )
            logger.info(
                "Player %s was banned due VAC history, last ban: %s days ago",
                str(player),
                bans.get("DaysSinceLastBan"),
            )
            rcon.do_perma_ban(player=name, reason=reason, by="VAC BOT")

            try:
                audit_params = dict(
                    player=name,
                    steam_id_64=player.steam_id_64,
                    reason=reason,
                    days_since_last_ban=bans.get("DaysSinceLastBan"),
                    vac_banned=bans.get("VACBanned"),
                    number_of_game_bans=bans.get("NumberOfGameBans"),
                )
                send_to_discord_audit(
                    f"`VAC/GAME BAN` -> {dict_to_discord(audit_params)}", "AUTOBAN"
                )
            except:
                logger.exception("Unable to send vac ban to audit log")


def inject_player_ids(func):
    @wraps(func)
    def wrapper(rcon, struct_log: StructuredLogLineType):
        name = struct_log["player"]
        steam_id_64 = struct_log["steam_id_64_1"]
        return func(rcon, struct_log, name, steam_id_64)

    return wrapper


@on_connected
@inject_player_ids
def handle_on_connect(rcon: Rcon, struct_log, name, steam_id_64):
    try:
        rcon.get_players.cache_clear()
        rcon.get_player_info.clear_for(struct_log["player"])
        rcon.get_player_info.clear_for(player=struct_log["player"])
    except Exception:
        logger.exception("Unable to clear cache for %s", steam_id_64)

    timestamp = int(struct_log["timestamp_ms"]) / 1000
    if not steam_id_64:
        logger.error(
            "Unable to get player steam ID for %s, can't process connection",
            struct_log,
        )
        return
    save_player(
        struct_log["player"],
        steam_id_64,
        timestamp=int(struct_log["timestamp_ms"]) / 1000,
    )
    save_start_player_session(steam_id_64, timestamp=timestamp)
    ban_if_blacklisted(rcon, steam_id_64, struct_log["player"])
    ban_if_has_vac_bans(rcon, steam_id_64, struct_log["player"])


@on_disconnected
@inject_player_ids
def handle_on_disconnect(rcon, struct_log, _, steam_id_64):
    save_end_player_session(steam_id_64, struct_log["timestamp_ms"] / 1000)


@on_connected
@inject_player_ids
def update_player_steaminfo_on_connect(rcon, struct_log, _, steam_id_64):
    if not steam_id_64:
        logger.error(
            "Can't update steam info, no steam id available for %s",
            struct_log.get("player"),
        )
        return
    profile = get_steam_profile(steam_id_64)
    if not profile:
        logger.error(
            "Can't update steam info, no steam profile returned for %s",
            struct_log.get("player"),
        )
        return

    logger.info("Updating steam profile for player %s", struct_log["player"])
    with enter_session() as sess:
        player = _get_set_player(
            sess, player_name=struct_log["player"], steam_id_64=steam_id_64
        )
        update_db_player_info(player, profile)
        sess.commit()


pendingTimers = {}


@on_connected
@inject_player_ids
def notify_false_positives(rcon: Rcon, _, name: str, steam_id_64: str):
    config = AutoModNoLeaderUserConfig.load_from_db()

    if not config.enabled:
        logger.info("no leader auto mod is disabled")
        return

    if not name.endswith(" "):
        return

    logger.info(
        "Detected player name with whitespace at the end: Warning them of false-positive events. Player name: "
        + name
    )

    try:
        send_to_discord_audit(
            f"WARNING Player with bugged profile joined: `{name}` `{steam_id_64}`\n\nThis player if Squad Officer will cause their squad to be punished. They also will show as unassigned in the Game view.\n\nPlease ask them to change their name (last character IG shouldn't be a whitespace)"
        )
    except Exception:
        logger.exception("Unable to send to audit")

    def notify_player():
        try:
            rcon.do_message_player(
                steam_id_64=steam_id_64,
                message=config.whitespace_message,
                by="CRcon",
                save_message=False,
            )
        except Exception as e:
            logger.error("Could not message player " + name + "/" + steam_id_64, e)

    # The player might not yet have finished connecting in order to send messages.
    t = Timer(10, notify_player)
    pendingTimers[steam_id_64] = t
    t.start()


@on_disconnected
@inject_player_ids
def cleanup_pending_timers(_, _1, _2, steam_id_64: str):
    pt: Timer = pendingTimers.pop(steam_id_64, None)
    if pt is None:
        return
    if pt.is_alive():
        try:
            pt.cancel()
        except:
            pass


def _set_real_vips(rcon: Rcon, struct_log):
    config = RealVipUserConfig.load_from_db()
    if not config.enabled:
        logger.debug("Real VIP is disabled")
        return

    desired_nb_vips = config.desired_total_number_vips
    min_vip_slot = config.minimum_number_vip_slots
    vip_count = rcon.get_vips_count()

    remaining_vip_slots = max(desired_nb_vips - vip_count, max(min_vip_slot, 0))
    rcon.set_vip_slots_num(remaining_vip_slots)
    logger.info("Real VIP set slots to %s", remaining_vip_slots)


@on_connected
def do_real_vips(rcon: Rcon, struct_log):
    _set_real_vips(rcon, struct_log)


@on_disconnected
def undo_real_vips(rcon: Rcon, struct_log):
    _set_real_vips(rcon, struct_log)


@on_camera
def notify_camera(rcon: Rcon, struct_log):
    send_to_discord_audit(message=struct_log["message"], by=struct_log["player"])

    try:
        if hooks := get_prepared_discord_hooks(CameraWebhooksUserConfig):
            embeded = DiscordEmbed(
                title=f'{struct_log["player"]}  - {struct_log["steam_id_64_1"]}',
                description=struct_log["sub_content"],
                color=242424,
            )
            for h in hooks:
                h.add_embed(embeded)
                h.execute()
    except Exception:
        logger.exception("Unable to forward to hooks")

    config = CameraNotificationUserConfig.load_from_db()
    if config.broadcast:
        temporary_broadcast(rcon, struct_log["message"], 60)

    if config.welcome:
        temporary_welcome(rcon, struct_log["message"], 60)


# ElGuillermo - feature add 1 - start
def _message_on_connect(rcon: Rcon, steam_id_64, struct_log):
    config = MessageOnConnectUserConfig.load_from_db()
    if not config.enabled:
        logger.debug("MessageOnConnect is disabled")
        return
    message_on_connect_txt = config.non_seed_time_text
    players_count_request = rcon.get_gamestate()
    players_count = (
        players_count_request["num_allied_players"]
        + players_count_request["num_axis_players"]
    )
    if players_count < config.seed_limit:
        message_on_connect_txt = config.seed_time_text
    player_name = struct_log["player"]

    def send_message_on_connect():
        try:
            rcon.do_message_player(
                steam_id_64=steam_id_64,
                message=message_on_connect_txt,
                by="Message_on_connect",
                save_message=False,
            )
        except Exception as e:
            logger.error(
                "Could not send MessageOnConnect to player "
                + "'" + player_name + "' ("
                + steam_id_64
                + ")",
                e
            )

    # The player might not yet have finished connecting in order to send messages.
    t = Timer(10, send_message_on_connect)
    pendingTimers[steam_id_64] = t
    t.start()

@on_connected
@inject_player_ids
def message_on_connect(rcon: Rcon, steam_id_64, struct_log):
    _message_on_connect(rcon, steam_id_64, struct_log["player"])
# ElGuillermo - feature add 1 - end
