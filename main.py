from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
)

import os
import json
import random
import re
import asyncio
import aiohttp
from bs4 import BeautifulSoup
import urllib.parse
import shutil
from typing import Dict, Tuple, Optional, NamedTuple, List
from pathlib import Path

# 在文件顶部（例如 import 之后）添加
_user_data_lock = asyncio.Lock()


async def download_and_parse_quiz(
    quiz_type: str,
    data_dir: Path,
    html_file: Optional[Path] = None,
    json_file: Optional[Path] = None,
    save_html: bool = True,
):
    """
    异步下载指定测验类型的 HTML 页面，提取题目信息，保存为 JSON。
    """
    if html_file is None:
        html_file = data_dir / f"{quiz_type}_quiz.html"
    if json_file is None:
        json_file = data_dir / f"{quiz_type}_quiz.json"

    url = f"https://foxquiz.com/zh/{quiz_type}-trivia-quiz"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # 下载 HTML
    html_content = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                html_content = await response.text()
        if save_html:
            with open(html_file, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"HTML 已保存至 {html_file}")
    except Exception as e:
        logger.error(f"下载 HTML 失败: {e}")
        # 尝试使用本地备份
        if html_file.exists():
            logger.info(f"使用本地文件 {html_file}")
            try:
                with open(html_file, "r", encoding="utf-8") as f:
                    html_content = f.read()
            except Exception as read_err:
                logger.error(f"读取本地 HTML 文件失败: {read_err}")
                raise
        else:
            raise

    if html_content is None:
        raise RuntimeError("无法获取 HTML 内容")

    # 解析 HTML
    soup = BeautifulSoup(html_content, "html.parser")
    cards = soup.select(
        "div.bg-white.rounded-lg.shadow-lg.overflow-hidden.border.border-gray-200"
    )

    quiz_data = []
    for idx, card in enumerate(cards, 1):
        h3 = card.select_one("h3.text-xl.font-semibold")
        if not h3:
            continue
        full_text = h3.get_text(strip=True)
        match = re.match(r"^(\d+)\s*(.*)", full_text)
        if match:
            number = match.group(1)
            question_text = match.group(2).strip()
        else:
            number = str(idx)
            question_text = full_text

        options = []
        correct_letter = None
        option_divs = card.select("div.p-4.rounded-lg.border")
        for opt in option_divs:
            letter_span = opt.select_one("span.leading-none")
            if not letter_span:
                continue
            letter = letter_span.get_text(strip=True)
            text_p = opt.select_one("p.text-lg")
            option_text = text_p.get_text(strip=True) if text_p else ""
            options.append({"letter": letter, "text": option_text})
            classes = opt.get("class", [])  # type: ignore
            if "bg-green-50" in classes and opt.select_one("svg.lucide-check"):  # type: ignore
                correct_letter = letter

        image_path = f"{number}.png"
        quiz_data.append(
            {
                "id": idx,
                "number": number,
                "question": question_text,
                "options": options,
                "correct_letter": correct_letter,
                "image": image_path,
            }
        )

    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(quiz_data, f, ensure_ascii=False, indent=2)

    logger.info(f"成功提取 {len(quiz_data)} 道题目，保存至 {json_file}")


BASE_URL = "https://foxquiz.com"




class ImageInfo(NamedTuple):
    """图片信息结构"""

    number: str
    url: str
    filepath: Path


def _extract_image_info(soup: BeautifulSoup, output_dir: Path) -> List[ImageInfo]:
    """从解析后的HTML中提取所有图片信息"""
    cards = soup.find_all(
        "div",
        class_="bg-white rounded-lg shadow-lg overflow-hidden border border-gray-200",
    )
    images_info = []
    for card in cards:
        # 提取序号（与原逻辑相同）
        title_div = card.find("div", class_="p-6 bg-gray-50 border-b border-gray-200")
        if not title_div:
            continue
        number_span = title_div.find(
            "span",
            class_=(
                "inline-flex items-center justify-center w-8 h-8 mr-3 "
                "rounded-full bg-gray-100 text-gray-700 text-sm font-semibold leading-none"
            ),
        )
        if not number_span:
            continue
        number = number_span.get_text(strip=True)

        # 提取图片元素
        img_div = card.find(
            "div",
            class_=(
                "relative w-full h-64 md:h-auto md:flex-1 md:min-h-[400px] "
                "bg-gray-100 order-1 md:order-2 border-b md:border-b-0 md:border-l border-gray-200"
            ),
        )
        if not img_div:
            continue
        img = img_div.find("img")
        if not img:
            continue

        # ---------- 关键修改：仿照示例代码处理 Next.js 代理 URL ----------
        src = img.get("src")
        if not src:
            srcset = img.get("srcset")
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]  # type: ignore # 取第一个 URL
        # 解析真实路径
        real_path = None
        if src and "/_next/image" in src:
            parsed = urllib.parse.urlparse(src) # type: ignore
            query_params = urllib.parse.parse_qs(parsed.query)
            if "url" in query_params:
                encoded_url = query_params["url"][0]
                real_path = urllib.parse.unquote(encoded_url)
        elif src:
            real_path = src

        if not real_path:
            logger.info(f"跳过序号 {number}: 无法解析图片URL")
            continue

        full_img_url = urllib.parse.urljoin(BASE_URL, real_path) # type: ignore
        # ---------------------------------------------------------------

        ext = os.path.splitext(real_path)[1] # type: ignore
        if not ext:
            ext = ".jpg"
        filename = f"{number}{ext}"
        filepath = output_dir / filename

        images_info.append(ImageInfo(number, full_img_url, filepath))

    return images_info


async def _download_single_image(
    session: aiohttp.ClientSession, url: str, filepath: Path, headers: Dict[str, str]
) -> None:
    """下载单张图片，异常已捕获，不会抛出"""
    try:
        async with session.get(
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            resp.raise_for_status()
            img_data = await resp.read()
        with open(filepath, "wb") as f:
            f.write(img_data)
        logger.debug(f"已下载: {filepath.name}")
    except Exception as e:
        logger.error(f"下载失败 {filepath.name}: {e}")


async def download_images(
    quiz_type: str, data_dir: Path, output_dir: Optional[Path] = None
) -> None:
    """
    异步并发下载指定测验类型的所有图片。
    下载完成后校验缺失文件，并自动重试一次。
    """
    html_file = data_dir / f"{quiz_type}_quiz.html"
    if output_dir is None:
        output_dir = data_dir / f"{quiz_type}_quiz_images"
    output_dir.mkdir(parents=True, exist_ok=True)

    url = f"https://foxquiz.com/zh/{quiz_type}-trivia-quiz"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # 获取 HTML
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                html_content = await response.text()
    except Exception as e:
        logger.error(f"获取网页失败: {e}")
        if html_file.exists():
            with open(html_file, "r", encoding="utf-8") as f:
                html_content = f.read()
            logger.info(f"使用本地HTML文件: {html_file}")
        else:
            logger.error(f"未找到{quiz_type}的本地html文件")
            raise

    soup = BeautifulSoup(html_content, "html.parser")
    # 提取所有图片信息
    images_info = _extract_image_info(soup, output_dir)
    if not images_info:
        logger.warning("未找到任何图片信息")
        return

    # 第一次下载：并发下载所有图片
    async with aiohttp.ClientSession() as session:
        tasks = []
        for info in images_info:
            # 如果文件已存在则跳过（避免重复下载）
            if info.filepath.exists():
                logger.info(f"图片 {info.filepath.name} 已存在，跳过")
                continue
            task = _download_single_image(session, info.url, info.filepath, headers)
            tasks.append(task)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 校验：检查缺失的文件
    missing = [info for info in images_info if not info.filepath.exists()]
    if missing:
        logger.warning(f"发现 {len(missing)} 个缺失文件，将重试下载一次")
        # 重试下载缺失的文件（仅一次）
        async with aiohttp.ClientSession() as session:
            retry_tasks = [
                _download_single_image(session, info.url, info.filepath, headers)
                for info in missing
            ]
            if retry_tasks:
                await asyncio.gather(*retry_tasks, return_exceptions=True)

        # 再次检查
        still_missing = [info for info in missing if not info.filepath.exists()]
        if still_missing:
            logger.error(
                f"重试后仍有 {len(still_missing)} 个文件下载失败: {[info.filepath.name for info in still_missing]}"
            )
        else:
            logger.info("所有缺失文件已成功重试下载")
    else:
        logger.info("所有图片下载完成")


def extract_random_questions(json_file_path: Path, num: int = 10):
    """从JSON题库文件中随机抽取指定数量的题目"""
    if not json_file_path.exists():
        raise FileNotFoundError(f"题库文件不存在：{json_file_path}")

    try:
        with open(json_file_path, "r", encoding="utf-8-sig") as f:
            questions = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析错误: {e}")
        logger.error(f"错误位置: 第 {e.lineno} 行, 第 {e.colno} 列")
        with open(json_file_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()
            start = max(0, e.lineno - 2)
            end = min(len(lines), e.lineno + 2)
            logger.error("上下文：")
            for i in range(start, end):
                prefix = ">>> " if i == e.lineno - 1 else "    "
                logger.error(prefix + lines[i].rstrip())
        raise
    except OSError as e:  # 新增：捕获文件读取错误
        logger.error(f"读取文件失败: {e}")
        raise RuntimeError(f"无法读取题库文件：{e}") from e
    except Exception as e:  # 新增：兜底其他未知错误
        logger.error(f"未知错误: {e}")
        raise

    if not isinstance(questions, list):
        raise ValueError("JSON 文件根元素应为数组")

    if len(questions) < num:
        selected = questions
    else:
        selected = random.sample(questions, num)

    result = []
    for q in selected:
        if "options" not in q or "correct_letter" not in q:
            logger.warning(f"警告：题目 {q.get('id', 'unknown')} 缺少必要字段，跳过")
            continue
        option_strings = [f"{opt['letter']}. {opt['text']}" for opt in q["options"]]
        result.append(
            {
                "id": q.get("id", 0),
                "number": q.get("number", str(q.get("id", 0))),  # 新增
                "question": q.get("question", ""),
                "options": option_strings,
                "correct": q["correct_letter"],
                "image": q.get("image", ""),
            }
)
    return result


async def _load_user_data(data_root: Path) -> Dict:
    file_path = data_root / "user_data.json"
    if not file_path.exists():
        return {"users": {}}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"users": {}}


async def _save_user_data(data_root: Path, data: Dict) -> None:
    file_path = data_root / "user_data.json"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def update_user_money(
    data_root: Path,
    user_id: str,
    user_name: str,
    quiz_type: str,
    current_game_money: int,
) -> Tuple[int, int]:
    async with _user_data_lock:
        try:
            data = await _load_user_data(data_root)
            if user_id not in data["users"]:
                data["users"][user_id] = {}
            user = data["users"][user_id]

            user["name"] = user_name

            if quiz_type not in user:
                user[quiz_type] = {"total_money": 0, "highest_record": 0}

            stats = user[quiz_type]
            stats["total_money"] += current_game_money
            if current_game_money > stats["highest_record"]:
                stats["highest_record"] = current_game_money

            await _save_user_data(data_root, data)
            return stats["total_money"], stats["highest_record"]
        except Exception as e:
            logger.error(
                f"更新用户数据失败: {e}\n"
                f"user_id={user_id}, quiz_type={quiz_type}, current_game_money={current_game_money}"
            )
            return 0, 0


async def get_user_stats(
    data_root: Path, user_id: str, quiz_type: str
) -> Tuple[int, int]:
    async with _user_data_lock:
        data = await _load_user_data(data_root)
        user = data["users"].get(user_id, {})
        stats = user.get(quiz_type, {"total_money": 0, "highest_record": 0})
        return stats["total_money"], stats["highest_record"]


async def get_user_all_stats(
    data_root: Path, user_id: str
) -> Dict[str, Dict[str, int]]:
    async with _user_data_lock:
        data = await _load_user_data(data_root)
        user_data = data["users"].get(user_id, {})
        stats_dict = {}
        for key, value in user_data.items():
            if (
                isinstance(value, dict)
                and "total_money" in value
                and "highest_record" in value
            ):
                stats_dict[key] = value
        return stats_dict


async def get_all_users_stats(data_root: Path) -> Dict[str, Dict[str, Dict[str, int]]]:
    async with _user_data_lock:
        data = await _load_user_data(data_root)
        all_stats = {}
        for uid, user_data in data["users"].items():
            stats_dict = {}
            for key, value in user_data.items():
                if (
                    isinstance(value, dict)
                    and "total_money" in value
                    and "highest_record" in value
                ):
                    stats_dict[key] = value
            if stats_dict:
                all_stats[uid] = stats_dict
        return all_stats


def list_available_quizzes(data_root: Path, validate_content: bool = False):
    """列出所有已下载并保存的可用测验题库"""
    if not data_root.is_dir():
        logger.warning(f"数据目录不存在: {data_root}")
        return []

    quizzes = []
    for filename in data_root.iterdir():
        if filename.name.endswith("_quiz.json"):
            json_path = data_root / filename
            quiz_type = filename.name[:-9]

            result = {
                "quiz_type": quiz_type,
                "json_file": str(json_path),
                "num_questions": 0,
                "valid": True,
            }

            if validate_content:
                try:
                    with open(json_path, "r", encoding="utf-8-sig") as f:
                        data = json.load(f)
                    if isinstance(data, list) and len(data) > 0:
                        result["num_questions"] = len(data)
                        result["valid"] = True
                    else:
                        result["valid"] = False
                        logger.warning(f"题库 {quiz_type} 内容格式错误或为空")
                except Exception as e:
                    result["valid"] = False
                    logger.warning(f"读取题库 {quiz_type} 失败: {e}")
            else:
                result["valid"] = True

            quizzes.append(result)

    return quizzes


def get_available_quiz_names(data_root: Path, validate_content: bool = False):
    """仅返回可用题库的名称列表（字符串）"""
    quizzes = list_available_quizzes(data_root, validate_content=validate_content)
    names = [q["quiz_type"] for q in quizzes if q["valid"]]
    # 使用正则一次清除所有无关字符
    cleaned_names = [re.sub(r"[\[\]'\"_]", "", name) for name in names]
    return cleaned_names


class QuizSession:
    def __init__(
        self,
        questions: list,
        data_dir: Path,
        quiz_type: str,
        user_id: str,
        user_name: str,
        image_paths: Optional[Dict[str, Path]] = None,
    ):
        self.questions = questions
        self.data_dir = data_dir
        self.quiz_type = quiz_type
        self.user_id = user_id
        self.user_name = user_name
        self.turn = 0
        self.money = 0
        self.chapter = 0
        self.image_paths = image_paths or {}  # 存储 id -> 图片路径

    async def send_question(self, event, controller=None) -> bool:
        q = self.questions[self.turn]
        options_text = "\n".join(q["options"])
        text = f"第{self.turn + 1}题：{q['question']}\n{options_text}"

        # 使用题目 number 字段获取图片路径（number 与文件名一致）
        img_path = self.image_paths.get(q["number"]) if self.image_paths else None
        if not img_path or not img_path.exists():
            logger.error(f"图片文件不存在或未配置: {q['number']}")
            return False  # 无法发送图片，游戏应结束

        try:
            await event.send(
                event.chain_result(
                    [Comp.Plain(text), Comp.Image.fromFileSystem(str(img_path))]
                )
            )
            logger.debug("题目发送成功（带图片）")
            if controller:
                controller.keep(timeout=15, reset_timeout=True)
            self.chapter = 0
            return True
        except Exception as e:
            logger.error(f"发送题目失败: {e}")
            return False



    async def handle_answer(
        self, event: AstrMessageEvent, controller: SessionController
    ) -> bool:
        answer = event.message_str.strip()
        if self.chapter == 1:
            if answer in ("退出", "quit"):
                await self._end_game(controller, event, exit_normally=True)
                return True
            elif answer == "继续":
                success = await self.send_question(event, controller)
                if not success:
                    await self._end_game(controller, event, send_failed=True)
                    return True
                return False
            else:
                # 无效输入，提示并继续等待
                await event.send(
                    event.plain_result("请输入“继续”继续挑战，或输入“退出”结束游戏。")
                )
                controller.keep(timeout=15, reset_timeout=True)
                return False
        else:
            if answer.upper() == str(self.questions[self.turn]["correct"]).upper():
                self.money += 50 * (self.turn + 1)
                self.turn += 1
                if self.turn == 10:
                    await self._end_game(controller, event, all_correct=True)
                    return True
                else:
                    await event.send(
                        event.plain_result(
                            f"奖池里面已经有{self.money}分，还要继续吗？你有15s时间考虑\n"
                            f"输入“继续”继续挑战，输入“退出”或“quit”带走。"
                        )
                    )
                    self.chapter = 1
                    controller.keep(timeout=15, reset_timeout=True)
                    return False
            else:
                await self._end_game(controller, event, wrong=True)
                return True

    async def _end_game(
        self, controller: SessionController, event: AstrMessageEvent, **kwargs
    ):
        """结束游戏并更新用户数据"""
        # 若因发送失败而结束，保留当前奖金
        if kwargs.get("send_failed"):
            final_money = self.money
            msg = f"发送题目时出错，游戏被迫结束。您已获得的 {final_money} 元已计入总奖金。"
        else:
            final_money = self.money if not kwargs.get("wrong") else 0

        new_total, new_highest = await update_user_money(
            self.data_dir, self.user_id, self.user_name, self.quiz_type, final_money
        )

        if kwargs.get("all_correct"):
            msg = f"挑战成功！获得全部奖金，奖池有{final_money}元！\n该类型累计总奖金：{new_total} 元，{self.quiz_type} 类型最高纪录：{new_highest} 元。"
        elif kwargs.get("wrong"):
            msg = f"回答错误!本次挑战结束，一共回答正确了{self.turn}个问题，奖池{final_money}元已经清零。\n您的该类型累计总奖金：{new_total} 元，{self.quiz_type} 类型最高纪录：{new_highest} 元。再接再厉！"
        elif kwargs.get("send_failed"):
            msg = f"题目发送失败，游戏终止。您已获得 {final_money} 元奖金。\n该类型累计总奖金：{new_total} 元，{self.quiz_type} 类型最高纪录：{new_highest} 元。"
        else:  # 正常退出
            msg = f"游戏结束，恭喜您本次挑战获得{final_money}元奖金。\n该类型题目累计总奖金：{new_total} 元，{self.quiz_type} 类型最高纪录：{new_highest} 元。"

        await event.send(event.plain_result(msg))
        controller.stop()


@register("Quiz", "cyh-x", "一个基于FoxQuiz网站的知识问答插件", "1.1.1")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        # 直接使用框架提供的数据目录，无需再拼接插件名
        self.data_dir = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

    async def initialize(self):
        """可选异步初始化"""
        pass

    @filter.command("quiz")
    async def quiz(self, event: AstrMessageEvent, type_of_quiz: str):
        """输入/quiz [题目类型]来开始问答"""
        user_name = event.get_sender_name()
        user_id = event.get_sender_id()
        file_path = self.data_dir / f"{type_of_quiz}_quiz.json"
        total_money, highest_record = await get_user_stats(
            self.data_dir, user_id, type_of_quiz
        )

        yield event.plain_result(
            f"欢迎{user_name}参加{type_of_quiz}个人问答挑战，共10题，每道题目奖金依次升高。但如果答错题目，奖金将清零！\n"
            f"您的累计奖金：{total_money} 元，本类型最高纪录：{highest_record} 元。"
        )

        try:
            random_questions = extract_random_questions(file_path, 10)
        except FileNotFoundError:
            yield event.plain_result(
                f"题库文件不存在：{file_path}。请确认是否下载了该题库，或者尝试使用unload删除题库后重新下载"
            )
            return
        except json.JSONDecodeError:
            yield event.plain_result(
                "题库文件格式错误，请检查 JSON 格式。或者尝试使用unload删除题库后重新下载"
            )
            return
        except (OSError, RuntimeError) as e:  # 新增：文件读取错误
            yield event.plain_result(f"读取题库文件失败：{e}")
            return
        except ValueError as e:  # 新增：内容结构错误
            yield event.plain_result(f"题库内容无效：{e}")
            return
        except Exception as e:  # 兜底
            yield event.plain_result(f"加载题库时发生未知错误：{e}")
            return


        images_dir = self.data_dir / f"{type_of_quiz}_quiz_images"
        image_paths = {}
        if images_dir.exists():
            for img_file in images_dir.iterdir():
                if img_file.is_file():
                    stem = img_file.stem          # 文件名不含扩展名，如 "76"
                    image_paths[stem] = img_file

        session = QuizSession(
            questions=random_questions,
            data_dir=self.data_dir,
            quiz_type=type_of_quiz,
            user_id=user_id,
            user_name=user_name,
            image_paths=image_paths   # 传入映射
        )

        # 发送第一题（此时没有 controller）
        # 发送第一题，并检查结果
        success = await session.send_question(event, controller=None)
        if not success:
            await event.send(event.plain_result("题目发送失败，游戏终止。"))
            return  # 不启动 handler
        @session_waiter(timeout=15, record_history_chains=False)
        async def handler(controller: SessionController, event: AstrMessageEvent):
            if event.get_sender_id() != user_id:
                return
            else:
                done = await session.handle_answer(event, controller)
                if done:
                    controller.stop()
            # 如果未结束，会话继续（内部已经调用 controller.keep 或已经通过 send_question 调用了 keep）

        try:
            await handler(event)
        except (TimeoutError, asyncio.TimeoutError):
            # 超时处理，注意 session 中有当前奖金，但超时后奖金清零
            final_money = 0
            new_total, new_highest = await update_user_money(
                self.data_dir, user_id, user_name, type_of_quiz, final_money
            )
            yield event.plain_result(
                f"你超时了！本次挑战结束，本次奖金已经清零。\n"
                f"累计总奖金：{new_total} 元，{type_of_quiz} 类型最高纪录：{new_highest} 元。"
            )
        except Exception as e:
            yield event.plain_result("发生错误，请联系管理员: " + str(e))

        finally:
            event.stop_event()

    @filter.command("quiz_load")
    async def quiz_load(self, event: AstrMessageEvent, type_of_quiz: str):
        """下载知识问答题库到本地"""
        if not type_of_quiz or not isinstance(type_of_quiz, str):
            yield event.plain_result("参数错误：题型名称不能为空。")
            return
        if any(c in type_of_quiz for c in ["/", "\\", "..", ".", "\0"]):
            yield event.plain_result("题型名称包含非法字符，请使用字母、数字或下划线。")
            return

        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            yield event.plain_result(f"创建数据目录失败: {str(e)}")
            return

        html_file = self.data_dir / f"{type_of_quiz}_quiz.html"
        json_file = self.data_dir / f"{type_of_quiz}_quiz.json"
        images_dir = self.data_dir / f"{type_of_quiz}_quiz_images"

        try:
            await download_and_parse_quiz(
                quiz_type=type_of_quiz,
                data_dir=self.data_dir,
                html_file=html_file,
                json_file=json_file,
            )
        except Exception as e:
            yield event.plain_result(f"下载题库失败: {str(e)}")
            return

        try:
            await download_images(
                quiz_type=type_of_quiz, data_dir=self.data_dir, output_dir=images_dir
            )
        except Exception as e:
            yield event.plain_result(f"下载图片失败: {str(e)}")
            return

        yield event.plain_result(f"{type_of_quiz}题库下载完成")

    @filter.command("quiz_unload")
    async def quiz_unload(self, event: AstrMessageEvent, quiz_type: str):
        """删除指定 quiz_type 的题库数据"""
        html_file = self.data_dir / f"{quiz_type}_quiz.html"
        json_file = self.data_dir / f"{quiz_type}_quiz.json"
        images_dir = self.data_dir / f"{quiz_type}_quiz_images"

        if html_file.exists():
            html_file.unlink()
            logger.info(f"已删除 HTML 文件: {html_file}")
        if json_file.exists():
            json_file.unlink()
            logger.info(f"已删除 JSON 文件: {json_file}")
        if images_dir.is_dir():
            shutil.rmtree(images_dir)
            logger.info(f"已删除图片文件夹: {images_dir}")

        yield event.plain_result(f"{quiz_type}题库卸载完成")

    @filter.command_group("quiz_stats")
    def quiz_stats(self):
        pass

    @quiz_stats.command("get")
    async def user_stats(self, event: AstrMessageEvent, target: str = None):  # type: ignore
        """查询用户统计信息"""
        if target:
            user_id = target
            user_name = target
        else:
            user_id = event.get_sender_id()
            user_name = event.get_sender_name()

        all_stats = await get_user_all_stats(self.data_dir, user_id)

        if not all_stats:
            yield event.plain_result(f"用户 {user_name} 暂无任何游戏记录。")
            return

        lines = [f"📊 用户：{user_name} 的问答统计", "=" * 20]
        for quiz_type, stats in all_stats.items():
            lines.append(f"【{quiz_type}】")
            lines.append(f"  累计奖金：{stats['total_money']} 元")
            lines.append(f"  最高纪录：{stats['highest_record']} 元")
            lines.append("-" * 20)
        total_all = sum(stats["total_money"] for stats in all_stats.values())
        lines.append(f"总奖金{total_all}元")
        result = "\n".join(lines)
        yield event.plain_result(f"{result}")
        # yield event.plain_result(f"总奖金{total_all}元")

    @quiz_stats.command("rank")
    async def user_rank(self, event: AstrMessageEvent, quiz_type: str = "all"):
        """查询用户排行榜"""
        users = await get_all_users_stats(self.data_dir)
        if not users:
            yield event.plain_result("暂无用户数据。")
            return

        rank_list = []
        for uid, stats in users.items():
            if quiz_type == "all":
                total = sum(stat.get("total_money", 0) for stat in stats.values())
            else:
                type_stats = stats.get(quiz_type)
                total = type_stats.get("total_money", 0) if type_stats else 0
            if total > 0:
                rank_list.append((uid, total))

        rank_list.sort(key=lambda x: x[1], reverse=True)

        if not rank_list:
            yield event.plain_result(f"暂无【{quiz_type}】类型的有奖记录。")
            return

        top_n = 10
        lines = [f"🏆 {quiz_type.upper()} 排行榜", "=" * 20]
        for idx, (uid, total) in enumerate(rank_list[:top_n], 1):
            lines.append(f"{idx}. 用户 ({uid})：{total} 元")
        if len(rank_list) > top_n:
            lines.append(f"... 共 {len(rank_list)} 人，仅显示前 {top_n} 名")
        result = "\n".join(lines)
        yield event.plain_result(result)

    @filter.command("quiz_list")
    async def quiz_list(self, event: AstrMessageEvent):
        """列出所有已下载并保存的可用测验题库"""
        names = get_available_quiz_names(self.data_dir, validate_content=False)
        if names:
            yield event.plain_result("可用题库：\n" + "\n".join(names))
        else:
            yield event.plain_result("暂无可用题库。")

    async def terminate(self):
        """插件销毁时调用"""
        pass
