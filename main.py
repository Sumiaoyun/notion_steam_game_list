import argparse
import requests
import time
import os
import logging
import json
from features.review import get_steam_review_info
from features.steamstore import get_steam_store_info

# CONFIG
STEAM_API_KEY = os.environ.get("STEAM_API_KEY")
STEAM_USER_ID = os.environ.get("STEAM_USER_ID")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")
include_played_free_games = os.environ.get("include_played_free_games") or 'true'
enable_item_update = os.environ.get("enable_item_update") or 'true'
enable_filter = os.environ.get("enable_filter") or 'false'

# 属性映射 - 根据数据库实际类型更新
PROPERTY_MAPPING = {
    "TITLE": "游戏名称",
    "PLAYTIME": "游玩时长 (h)",
    "LAST_PLAYED": "上次游玩时间",
    "STORE_URL": "商店链接",
    "COMPLETION": "完成度",
    "TOTAL_ACHIEVEMENTS": "总成就数",
    "ACHIEVED_ACHIEVEMENTS": "已完成成就数",
    "REVIEW": "评测",
    "INFO": "游戏简介",
    "TAGS": "游戏标签"
}

# 属性类型映射 - 根据错误信息更新
PROPERTY_TYPES = {
    "游戏名称": "title",
    "游玩时长 (h)": "number",
    "上次游玩时间": "date",
    "商店链接": "url",
    "完成度": "multi_select",
    "总成就数": "number",
    "已完成成就数": "number",
    "评测": "rich_text",
    "游戏简介": "rich_text",
    "游戏标签": "multi_select"  # 根据错误信息修正为多选类型
}

# MISC
MAX_RETRIES = 20
RETRY_DELAY = 2

logger = logging.getLogger(__name__)

def send_request_with_retry(url, headers=None, json_data=None, params=None, retries=MAX_RETRIES, method="patch"):
    response = None
    while retries > 0:
        try:
            if method == "patch":
                response = requests.patch(url, headers=headers, json=json_data, params=params)
            elif method == "post":
                response = requests.post(url, headers=headers, json=json_data, params=params)
            elif method == "get":
                response = requests.get(url, headers=headers, params=params)

            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            error_text = response.text if response is not None else "No response"
            logger.error(f"请求异常: <{e}> .错误: {error_text}, 重试中....")
            retries -= 1
            if retries > 0:
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"超过最大重试次数.错误: {error_text}, 放弃.")
                return {}
    return {}

def validate_database_structure():
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Notion-Version": "2022-06-28",
    }
    
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        database = response.json()
        
        # 检查属性类型是否匹配
        for prop_name, prop_type in PROPERTY_TYPES.items():
            if prop_name in database["properties"]:
                db_prop_type = database["properties"][prop_name]["type"]
                if db_prop_type != prop_type:
                    logger.warning(f"属性 '{prop_name}' 类型不匹配: 数据库是 {db_prop_type}, 代码期望 {prop_type}")
            else:
                logger.warning(f"数据库中缺少属性: {prop_name}")
        
        logger.info("数据库结构验证完成")
        return True
    except Exception as e:
        logger.error(f"验证数据库结构失败: {str(e)}")
        return False

def get_owned_game_data_from_steam():
    url = "http://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": STEAM_USER_ID,
        "include_appinfo": True,
        "format": "json"
    }
    
    if include_played_free_games == "true":
        params["include_played_free_games"] = True

    logger.info("从Steam获取数据中..")

    try:
        response = send_request_with_retry(url, params=params, method="get")
        if response:
            logger.info("数据获取成功!")
            return response.json()
        else:
            logger.error("获取Steam数据失败: 无响应")
            return {}
    except Exception as e:
        logger.error(f"获取Steam数据失败: {e}")
        return {}

def query_achievements_info_from_steam(game):
    url = "http://api.steampowered.com/ISteamUserStats/GetPlayerAchievements/v0001/"
    params = {
        "key": STEAM_API_KEY,
        "steamid": STEAM_USER_ID,
        "appid": game['appid']
    }
    
    logger.info(f"查询游戏成就数据: {game['name']}")

    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        error_text = response.text if hasattr(response, 'text') else str(e)
        logger.error(f"查询成就失败: {game['name']}: {str(e)} .错误: {error_text}")
    except ValueError as e:
        logger.error(f"解析JSON响应失败: {game['name']}: {str(e)}")
    return None

def get_achievements_count(game):
    game_achievements = query_achievements_info_from_steam(game)
    achievements_info = {}
    achievements_info["total"] = 0
    achievements_info["achieved"] = 0

    if game_achievements is None or game_achievements.get("playerstats", {}).get("success", False) is False:
        achievements_info["total"] = -1
        achievements_info["achieved"] = -1
        logger.info(f"游戏无成就信息: {game['name']}")

    elif "achievements" not in game_achievements["playerstats"]:
        achievements_info["total"] = -1
        achievements_info["achieved"] = -1
        logger.info(f"游戏无成就: {game['name']}")

    else:
        achievements_array = game_achievements["playerstats"]["achievements"]
        for achievement_dict in achievements_array:
            achievements_info["total"] = achievements_info["total"] + 1
            if achievement_dict["achieved"]:
                achievements_info["achieved"] = achievements_info["achieved"] + 1

        logger.info(f"{game['name']} 成就统计完成!")

    return achievements_info

def is_record(game, achievements_info):
    not_record_time = "2020-01-01 00:00:00"
    time_tuple = time.strptime(not_record_time, "%Y-%m-%d %H:%M:%S")
    timestamp = time.mktime(time_tuple)
    playtime = round(float(game["playtime_forever"]) / 60, 1)

    if (playtime < 0.1 and achievements_info["total"] < 1) or (
        game.get("rtime_last_played", 0) < timestamp
        and achievements_info["total"] < 1
        and playtime < 6
    ):
        logger.info(f"{game['name']} 不符合过滤规则!")
        return False

    return True

def add_item_to_notion_database(game, achievements_info, review_text, steam_store_data):
    url = "https://api.notion.com/v1/pages"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    logger.info(f"添加游戏到Notion: {game['name']}")

    playtime = round(float(game["playtime_forever"]) / 60, 1)
    last_played_time = time.strftime("%Y-%m-%d", time.localtime(game.get("rtime_last_played", 0)))
    store_url = f"https://store.steampowered.com/app/{game['appid']}"
    icon_url = f"https://media.steampowered.com/steamcommunity/public/images/apps/{game['appid']}/{game['img_icon_url']}.jpg"
    cover_url = f"https://steamcdn-a.akamaihd.net/steam/apps/{game['appid']}/header.jpg"
    
    total_achievements = achievements_info.get("total", 0)
    achieved_achievements = achievements_info.get("achieved", 0)
    
    completion = -1
    if total_achievements > 0:
        completion = round(float(achieved_achievements) / float(total_achievements) * 100, 1)

    # 根据数据库实际类型调整属性格式
    properties = {
        PROPERTY_MAPPING["TITLE"]: {
            "type": "title",
            "title": [{"type": "text", "text": {"content": game['name']}}]
        },
        PROPERTY_MAPPING["PLAYTIME"]: {"type": "number", "number": playtime},
        PROPERTY_MAPPING["LAST_PLAYED"]: {"type": "date", "date": {"start": last_played_time}},
        PROPERTY_MAPPING["STORE_URL"]: {"type": "url", "url": store_url},
        PROPERTY_MAPPING["TOTAL_ACHIEVEMENTS"]: {"type": "number", "number": total_achievements},
        PROPERTY_MAPPING["ACHIEVED_ACHIEVEMENTS"]: {"type": "number", "number": achieved_achievements},
        PROPERTY_MAPPING["REVIEW"]: {
            "type": "rich_text",
            "rich_text": [{"type": "text", "text": {"content": review_text}}]
        },
        PROPERTY_MAPPING["INFO"]: {
            "type": "rich_text",
            "rich_text": [{"type": "text", "text": {"content": steam_store_data.get("info", "")}}]
        }
    }
    
    # 调整完成度属性为多选类型
    if PROPERTY_TYPES.get(PROPERTY_MAPPING["COMPLETION"], "") == "multi_select":
        completion_value = []
        if completion >= 0:
            completion_value = [{"name": f"{completion}%"}]
        properties[PROPERTY_MAPPING["COMPLETION"]] = {
            "type": "multi_select",
            "multi_select": completion_value
        }
    else:  # 默认使用数字类型
        properties[PROPERTY_MAPPING["COMPLETION"]] = {"type": "number", "number": completion}
    
    # 调整游戏标签属性为多选类型
    if PROPERTY_TYPES.get(PROPERTY_MAPPING["TAGS"], "") == "checkbox":
        has_tags = len(steam_store_data.get('tag', [])) > 0
        properties[PROPERTY_MAPPING["TAGS"]] = {
            "type": "checkbox",
            "checkbox": has_tags
        }
    else:  # 默认使用多选类型
        # 确保标签格式正确 - 每个标签应该是 {"name": "标签名称"} 格式
        tags = []
        for tag in steam_store_data.get('tag', []):
            if isinstance(tag, dict):
        # 如果标签已经是字典格式，确保它有 'name' 字段
                if 'name' in tag:
                    tags.append({"name": tag['name']})
                else:
        # 处理没有 'name' 字段的情况
                    logger.warning(f"无效标签格式: {tag}")
            else:
        # 如果标签是字符串，直接使用
                tags.append({"name": str(tag)})
        properties[PROPERTY_MAPPING["TAGS"]] = {
            "type": "multi_select",
            "multi_select": tags
        }

    data = {
        "parent": {"type": "database_id", "database_id": NOTION_DATABASE_ID},
        "properties": properties,
        "cover": {"type": "external", "external": {"url": cover_url}},
        "icon": {"type": "external", "external": {"url": icon_url}},
    }

    try:
        response = send_request_with_retry(url, headers=headers, json_data=data, method="post")
        if response:
            logger.info(f"{game['name']} 添加成功!")
            return response.json()
        return {}
    except Exception as e:
        logger.error(f"添加失败: {e}")
        return {}

def query_item_from_notion_database(game):
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    logger.info(f"在数据库中查询游戏: {game['name']}")
    
    data = {
        "filter": {
            "property": PROPERTY_MAPPING["TITLE"],
            "title": {
                "equals": game['name']
            }
        }
    }

    try:
        response = send_request_with_retry(
            url, headers=headers, json_data=data, method="post"
        )
        if response:
            logger.info("查询完成!")
            return response.json()
        return {"results": []}
    except Exception as e:
        logger.error(f"查询失败: {e}")
        return {"results": []}

def update_item_to_notion_database(page_id, game, achievements_info, review_text, steam_store_data):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    headers = {
        "Authorization": f"Bearer {NOTION_API_KEY}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    playtime = round(float(game["playtime_forever"]) / 60, 1)
    last_played_time = time.strftime("%Y-%m-%d", time.localtime(game.get("rtime_last_played", 0)))
    store_url = f"https://store.steampowered.com/app/{game['appid']}"
    icon_url = f"https://media.steampowered.com/steamcommunity/public/images/apps/{game['appid']}/{game['img_icon_url']}.jpg"
    cover_url = f"https://steamcdn-a.akamaihd.net/steam/apps/{game['appid']}/header.jpg"
    
    total_achievements = achievements_info.get("total", 0)
    achieved_achievements = achievements_info.get("achieved", 0)
    
    completion = -1
    if total_achievements > 0:
        completion = round(float(achieved_achievements) / float(total_achievements) * 100, 1)

    logger.info(f"更新游戏信息: {game['name']}")

    # 根据数据库实际类型调整属性格式
    properties = {
        PROPERTY_MAPPING["TITLE"]: {
            "type": "title",
            "title": [{"type": "text", "text": {"content": game['name']}}]
        },
        PROPERTY_MAPPING["PLAYTIME"]: {"type": "number", "number": playtime},
        PROPERTY_MAPPING["LAST_PLAYED"]: {"type": "date", "date": {"start": last_played_time}},
        PROPERTY_MAPPING["STORE_URL"]: {"type": "url", "url": store_url},
        PROPERTY_MAPPING["TOTAL_ACHIEVEMENTS"]: {"type": "number", "number": total_achievements},
        PROPERTY_MAPPING["ACHIEVED_ACHIEVEMENTS"]: {"type": "number", "number": achieved_achievements},
        PROPERTY_MAPPING["REVIEW"]: {
            "type": "rich_text",
            "rich_text": [{"type": "text", "text": {"content": review_text}}]
        },
        PROPERTY_MAPPING["INFO"]: {
            "type": "rich_text",
            "rich_text": [{"type": "text", "text": {"content": steam_store_data.get("info", "")}}]
        }
    }
    
    # 调整完成度属性为多选类型
    if PROPERTY_TYPES.get(PROPERTY_MAPPING["COMPLETION"], "") == "multi_select":
        completion_value = []
        if completion >= 0:
            completion_value = [{"name": f"{completion}%"}]
        properties[PROPERTY_MAPPING["COMPLETION"]] = {
            "type": "multi_select",
            "multi_select": completion_value
        }
    else:  # 默认使用数字类型
        properties[PROPERTY_MAPPING["COMPLETION"]] = {"type": "number", "number": completion}
    
    # 调整游戏标签属性为多选类型
    if PROPERTY_TYPES.get(PROPERTY_MAPPING["TAGS"], "") == "checkbox":
        has_tags = len(steam_store_data.get('tag', [])) > 0
        properties[PROPERTY_MAPPING["TAGS"]] = {
            "type": "checkbox",
            "checkbox": has_tags
        }
    else:  # 默认使用多选类型
        # 确保标签格式正确 - 每个标签应该是 {"name": "标签名称"} 格式
            tags = []
            for tag in steam_store_data.get('tag', []):
                if isinstance(tag, dict):
        # 如果标签已经是字典格式，确保它有 'name' 字段
                    if 'name' in tag:
                        tags.append({"name": tag['name']})
                    else:
        # 处理没有 'name' 字段的情况
                        logger.warning(f"无效标签格式: {tag}")
                else:
        # 如果标签是字符串，直接使用
                    tags.append({"name": str(tag)})
            properties[PROPERTY_MAPPING["TAGS"]] = {
                "type": "multi_select",
                "multi_select": tags
            }

    data = {
        "properties": properties,
        "cover": {"type": "external", "external": {"url": cover_url}},
        "icon": {"type": "external", "external": {"url": icon_url}},
    }

    try:
        response = send_request_with_retry(url, headers=headers, json_data=data, method="patch")
        if response:
            logger.info(f"{game['name']} 更新成功!")
            return response.json()
        return {}
    except Exception as e:
        logger.error(f"更新失败: {e}")
        return {}

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true', help='启用调试日志输出')
    args = parser.parse_args()

    # 配置日志
    logger = logging.getLogger("")
    logger.setLevel(logging.DEBUG if args.debug else logging.INFO)
    
    # 创建格式化器
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器（仅在调试模式启用）
    if args.debug:
        file_handler = logging.FileHandler("app.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    # 记录环境变量配置
    logger.debug("环境变量配置:")
    logger.debug(f"STEAM_API_KEY: {STEAM_API_KEY[:4]}...")
    logger.debug(f"STEAM_USER_ID: {STEAM_USER_ID}")
    logger.debug(f"NOTION_API_KEY: {NOTION_API_KEY[:6]}...")
    logger.debug(f"NOTION_DATABASE_ID: {NOTION_DATABASE_ID}")
    logger.debug(f"include_played_free_games: {include_played_free_games}")
    logger.debug(f"enable_item_update: {enable_item_update}")
    logger.debug(f"enable_filter: {enable_filter}")
    
    # 添加数据库验证
    if not validate_database_structure():
        logger.error("数据库结构验证失败，请检查属性配置")
        exit(1)
        
    owned_game_data = get_owned_game_data_from_steam()
    
    if not owned_game_data or "response" not in owned_game_data or "games" not in owned_game_data["response"]:
        logger.error("无法获取Steam游戏数据，请检查API密钥和用户ID")
        exit(1)
    
    for game in owned_game_data["response"]["games"]:
        achievements_info = get_achievements_count(game)
        review_text = get_steam_review_info(game["appid"], STEAM_USER_ID)
        steam_store_data = get_steam_store_info(game["appid"])
        logger.info(f"{game['name']} 评测: {review_text}")

        if "rtime_last_played" not in game:
            logger.info(f"{game['name']} 无最后游玩时间! 设置为0")
            game["rtime_last_played"] = 0

        if enable_filter == "true" and not is_record(game, achievements_info):
            continue

        queryed_item = query_item_from_notion_database(game)
        if "results" not in queryed_item:
            logger.error(f"{game['name']} 查询失败! 跳过")
            continue

        if queryed_item["results"]:
            if enable_item_update == "true":
                logger.info(f"{game['name']} 已存在! 更新中...")
                update_item_to_notion_database(
                    queryed_item["results"][0]["id"], game, achievements_info, review_text, steam_store_data
                )
            else:
                logger.info(f"{game['name']} 已存在! 跳过")
        else:
            logger.info(f"{game['name']} 不存在! 创建新条目")
            add_item_to_notion_database(game, achievements_info, review_text, steam_store_data)
