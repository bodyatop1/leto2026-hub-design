import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from app.storage import load_quiz_config


@dataclass
class Player:
    session_token: str
    nickname: str
    team_id: str
    is_curator: bool = False
    sid: Optional[str] = None


@dataclass
class Team:
    id: str
    name: str
    color: str = "#2563eb"
    is_organizers: bool = False
    score: int = 0
    curator_token: Optional[str] = None
    members: Dict[str, Player] = field(default_factory=dict)
    votes: Dict[str, str] = field(default_factory=dict)
    submitted_option: Optional[str] = None
    submitted_text: str = ""
    verdict: Optional[str] = None  # "correct", "partial", "wrong", or None
    awarded_points: int = 0
    curator_change_allowed: bool = False
    question_answers: Dict[str, str] = field(default_factory=dict)
    question_verdicts: Dict[str, str] = field(default_factory=dict)


class QuizEngine:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.status: str = "lobby"
        self.current_index: int = -1
        self.question_ends_at: Optional[float] = None
        self.podium_reveal_count: int = 0
        self.teams: Dict[str, Team] = {}
        self.players: Dict[str, Player] = {}
        self.sync_teams_from_config()

    def get_config(self) -> dict:
        return load_quiz_config()

    def sync_teams_from_config(self) -> None:
        cfg = self.get_config()
        teams_cfg = cfg.get("teams_config", [])
        new_teams: Dict[str, Team] = {}
        for item in teams_cfg:
            tid = str(item.get("id", "")).strip()
            if not tid:
                continue
            name = str(item.get("name", tid)).strip() or tid
            color = str(item.get("color", "#2563eb")).strip() or "#2563eb"
            is_org = bool(item.get("is_organizers", tid == "organizers"))
            if tid in self.teams:
                t = self.teams[tid]
                t.name = name
                t.color = color
                t.is_organizers = is_org
                new_teams[tid] = t
            else:
                new_teams[tid] = Team(id=tid, name=name, color=color, is_organizers=is_org)
        removed_ids = set(self.teams.keys()) - set(new_teams.keys())
        for tok, pl in list(self.players.items()):
            if pl.team_id in removed_ids:
                del self.players[tok]
        self.teams = new_teams

    def get_team_capacity(self) -> int:
        return max(1, int(self.get_config().get("team_capacity", 40)))

    def is_organizers_enabled(self) -> bool:
        return bool(self.get_config().get("organizers_enabled", True))

    def is_organizers_off_podium(self) -> bool:
        return bool(self.get_config().get("organizers_off_podium", True))

    def is_auto_random_unanswered(self) -> bool:
        return bool(self.get_config().get("auto_random_unanswered", False))

    def active_team_ids(self) -> List[str]:
        self.sync_teams_from_config()
        org_on = self.is_organizers_enabled()
        return [
            tid for tid, t in self.teams.items()
            if (not t.is_organizers) or org_on
        ]

    def get_questions(self) -> List[dict]:
        return self.get_config().get("questions", [])

    def get_current_question(self) -> Optional[dict]:
        questions = self.get_questions()
        if 0 <= self.current_index < len(questions):
            return questions[self.current_index]
        return None

    def _option_keys(self, question: Optional[dict]) -> List[str]:
        if not question:
            return []
        return [str(o.get("key", "")) for o in question.get("options", []) if o.get("key")]

    def get_team_vote_counts(self, team: Team, question: Optional[dict] = None) -> Dict[str, int]:
        keys = self._option_keys(question or self.get_current_question())
        counts = {k: 0 for k in keys}
        for opt in team.votes.values():
            if opt in counts:
                counts[opt] += 1
        return counts

    def get_leading_option(self, team: Team, question: Optional[dict] = None) -> Optional[str]:
        counts = self.get_team_vote_counts(team, question)
        best_opt = None
        best_val = 0
        for opt, val in counts.items():
            if val > best_val:
                best_val = val
                best_opt = opt
        return best_opt

    def all_active_teams_submitted(self) -> bool:
        q = self.get_current_question()
        if not q:
            return False
        q_type = q.get("question_type", "choice")
        active_ids = self.active_team_ids()
        if not active_ids:
            return False
        for tid in active_ids:
            t = self.teams.get(tid)
            if not t:
                continue
            if q_type == "open":
                if not t.submitted_text.strip():
                    return False
            else:
                if not t.submitted_option:
                    return False
        return True

    async def join_team(
        self,
        session_token: str,
        nickname: str,
        team_id: str,
        sid: str,
        force_curator: bool = False,
    ) -> dict:
        async with self.lock:
            self.sync_teams_from_config()
            if team_id not in self.teams:
                return {"ok": False, "error": "Команда не найдена"}
            target_team = self.teams[team_id]
            if target_team.is_organizers and not self.is_organizers_enabled():
                return {"ok": False, "error": "Команда Организаторы сейчас отключена"}

            nickname = nickname.strip()[:30]
            if not nickname:
                return {"ok": False, "error": "Введите имя"}

            cap = self.get_team_capacity()
            existing = self.players.get(session_token)

            if existing and existing.team_id != team_id and existing.team_id in self.teams:
                old_team = self.teams[existing.team_id]
                old_team.members.pop(session_token, None)
                old_team.votes.pop(session_token, None)
                if old_team.curator_token == session_token:
                    old_team.curator_token = next(iter(old_team.members), None)
                    if old_team.curator_token:
                        old_team.members[old_team.curator_token].is_curator = True
                existing = None

            if not existing and len(target_team.members) >= cap:
                return {"ok": False, "error": f"В команде уже максимум ({cap} человек)"}

            if force_curator:
                for m in target_team.members.values():
                    m.is_curator = False
                target_team.curator_token = session_token
                is_curator = True
            elif existing:
                is_curator = (target_team.curator_token == session_token)
            else:
                is_curator = (target_team.curator_token is None)
                if is_curator:
                    target_team.curator_token = session_token

            player = Player(
                session_token=session_token,
                nickname=nickname,
                team_id=team_id,
                is_curator=is_curator,
                sid=sid,
            )
            target_team.members[session_token] = player
            self.players[session_token] = player
            return {"ok": True, "player": player}

    async def set_curator(self, team_id: str, session_token: str) -> dict:
        async with self.lock:
            team = self.teams.get(team_id)
            if not team or session_token not in team.members:
                return {"ok": False, "error": "Участник не найден"}
            for m in team.members.values():
                m.is_curator = False
            team.curator_token = session_token
            team.members[session_token].is_curator = True
            return {"ok": True}

    async def cast_vote(self, session_token: str, option_key: str) -> dict:
        async with self.lock:
            if self.status != "question":
                return {"ok": False, "error": "Приём голосов сейчас закрыт"}
            q = self.get_current_question()
            if not q or q.get("question_type") == "open":
                return {"ok": False, "error": "Для этого вопроса голоса участников не используются"}
            player = self.players.get(session_token)
            if not player or player.team_id not in self.teams:
                return {"ok": False, "error": "Сначала войдите в команду"}
            team = self.teams[player.team_id]
            if team.is_organizers and not self.is_organizers_enabled():
                return {"ok": False, "error": "Команда Организаторы отключена"}
            if team.submitted_option is not None and not team.curator_change_allowed:
                return {"ok": False, "error": "Куратор уже отправил ответ команды"}
            valid_keys = set(self._option_keys(q))
            if option_key not in valid_keys:
                return {"ok": False, "error": "Неверный вариант"}

            team.votes[session_token] = option_key
            return {"ok": True, "team_id": team.id}

    async def curator_submit(
        self,
        session_token: str,
        option_key: Optional[str] = None,
        answer_text: Optional[str] = None,
    ) -> dict:
        async with self.lock:
            if self.status != "question":
                return {"ok": False, "error": "Сейчас нельзя отправить ответ"}
            q = self.get_current_question()
            if not q:
                return {"ok": False, "error": "Вопрос не найден"}
            player = self.players.get(session_token)
            if not player or not player.is_curator or player.team_id not in self.teams:
                return {"ok": False, "error": "Отправить ответ команды может только куратор"}
            team = self.teams[player.team_id]
            if team.is_organizers and not self.is_organizers_enabled():
                return {"ok": False, "error": "Команда Организаторы отключена"}

            q_type = q.get("question_type", "choice")
            if q_type == "open":
                if team.submitted_text.strip() and not team.curator_change_allowed:
                    return {"ok": False, "error": "Ответ команды уже отправлен"}
                txt = (answer_text or "").strip()
                if not txt:
                    return {"ok": False, "error": "Введите свободный ответ команды"}
                team.submitted_text = txt
                team.curator_change_allowed = False
                if q.get("id"):
                    team.question_answers[q["id"]] = txt
                return {"ok": True, "team_id": team.id, "answer_text": txt}

            if team.submitted_option is not None and not team.curator_change_allowed:
                return {"ok": False, "error": "Ответ команды уже отправлен"}

            valid_keys = set(self._option_keys(q))
            chosen = option_key if option_key in valid_keys else self.get_leading_option(team, q)
            if not chosen:
                return {"ok": False, "error": "Выберите вариант для отправки"}

            team.submitted_option = chosen
            team.curator_change_allowed = False
            if q.get("id"):
                team.question_answers[q["id"]] = chosen
            return {"ok": True, "team_id": team.id, "option": chosen}

    async def grade_open_answer(self, team_id: str, verdict: str) -> dict:
        async with self.lock:
            team = self.teams.get(team_id)
            q = self.get_current_question()
            if not team or not q:
                return {"ok": False, "error": "Команда или вопрос не найдены"}
            if verdict not in ("correct", "partial", "wrong"):
                return {"ok": False, "error": "Неверная оценка"}

            full_pts = int(q.get("points", 100) or 100)
            part_pts = int(q.get("partial_points", max(10, full_pts // 2)) or 0)
            target_pts = full_pts if verdict == "correct" else (part_pts if verdict == "partial" else 0)

            team.score -= team.awarded_points
            team.score += target_pts
            team.awarded_points = target_pts
            team.verdict = verdict
            if q.get("id"):
                team.question_verdicts[q["id"]] = verdict
            return {"ok": True}

    async def allow_curator_change(self, team_id: str) -> dict:
        async with self.lock:
            team = self.teams.get(team_id)
            if not team:
                return {"ok": False, "error": "Команда не найдена"}
            team.curator_change_allowed = True
            return {"ok": True}

    async def override_team_result(
        self,
        team_id: str,
        new_option: str,
        new_score: int,
        question_id: Optional[str] = None,
        new_text: Optional[str] = None,
    ) -> dict:
        async with self.lock:
            team = self.teams.get(team_id)
            if not team:
                return {"ok": False, "error": "Команда не найдена"}
            cur_q = self.get_current_question()
            target_qid = question_id or (cur_q.get("id") if cur_q else None)
            if new_text is not None and new_text.strip():
                team.submitted_text = new_text.strip()
                if target_qid:
                    team.question_answers[target_qid] = new_text.strip()
            else:
                normalized_opt = new_option.strip() if new_option else None
                if target_qid:
                    team.question_answers[target_qid] = normalized_opt or ""
                if not target_qid or (cur_q and cur_q.get("id") == target_qid):
                    team.submitted_option = normalized_opt
            team.score = int(new_score)
            return {"ok": True}

    async def start_quiz(self) -> dict:
        async with self.lock:
            self.sync_teams_from_config()
            cfg = self.get_config()
            self.current_index = -1
            self.question_ends_at = None
            self.podium_reveal_count = 0
            for team in self.teams.values():
                team.score = 0
                team.votes.clear()
                team.submitted_option = None
                team.submitted_text = ""
                team.verdict = None
                team.awarded_points = 0
                team.curator_change_allowed = False
                team.question_answers.clear()
                team.question_verdicts.clear()

            if cfg.get("sponsor_enabled", True):
                self.status = "sponsor"
                return {"ok": True, "status": self.status}

            return await self._activate_next_question_locked()

    async def _activate_next_question_locked(self) -> dict:
        questions = self.get_questions()
        if self.current_index + 1 >= len(questions):
            return await self._enter_final_locked()

        self.current_index += 1
        self.status = "question"
        q = questions[self.current_index]
        time_limit = max(5, int(q.get("time_limit", 30)))
        self.question_ends_at = time.time() + time_limit

        for team in self.teams.values():
            team.votes.clear()
            team.submitted_option = None
            team.submitted_text = ""
            team.verdict = None
            team.awarded_points = 0
            team.curator_change_allowed = False
        return {"ok": True, "status": self.status}

    async def start_next_question(self) -> dict:
        async with self.lock:
            if self.status == "lobby":
                cfg = self.get_config()
                if cfg.get("sponsor_enabled", True):
                    self.status = "sponsor"
                    return {"ok": True, "status": self.status}
            return await self._activate_next_question_locked()

    async def close_question_and_score(self) -> dict:
        async with self.lock:
            if self.status != "question":
                return {"ok": False, "error": "Нет активного вопроса"}
            question = self.get_current_question()
            if not question:
                return {"ok": False, "error": "Вопрос не найден"}

            qid = question.get("id", "")
            q_type = question.get("question_type", "choice")
            points = int(question.get("points", 100))
            active_ids = set(self.active_team_ids())
            random_fill = self.is_auto_random_unanswered()

            if q_type == "open":
                for tid, team in self.teams.items():
                    if tid not in active_ids:
                        continue
                    if not team.submitted_text.strip() and random_fill:
                        team.submitted_text = "Тестовый свободный ответ"
                    if qid:
                        team.question_answers[qid] = team.submitted_text
                self.status = "reveal"
                self.question_ends_at = None
                return {"ok": True, "status": self.status}

            correct = question.get("correct_option")
            valid_keys = self._option_keys(question)

            for tid, team in self.teams.items():
                if tid not in active_ids:
                    continue
                if team.submitted_option is None:
                    leading = self.get_leading_option(team, question)
                    if leading:
                        team.submitted_option = leading
                    elif random_fill and valid_keys:
                        team.submitted_option = random.choice(valid_keys)
                if qid:
                    team.question_answers[qid] = team.submitted_option or ""
                if team.submitted_option == correct:
                    team.score += points
                    team.awarded_points = points
                    team.verdict = "correct"
                else:
                    team.awarded_points = 0
                    team.verdict = "wrong"
                if qid:
                    team.question_verdicts[qid] = team.verdict
                team.curator_change_allowed = False

            self.status = "reveal"
            self.question_ends_at = None
            return {"ok": True, "status": self.status}

    async def _enter_final_locked(self) -> dict:
        cfg = self.get_config()
        self.question_ends_at = None
        if cfg.get("stepwise_podium_enabled", True):
            self.status = "podium_step"
            self.podium_reveal_count = 1
        else:
            self.status = "finished"
            self.podium_reveal_count = len(self.active_team_ids())
        return {"ok": True, "status": self.status}

    async def finish_quiz(self) -> dict:
        async with self.lock:
            return await self._enter_final_locked()

    async def reveal_next_podium_step(self) -> dict:
        async with self.lock:
            place_prizes = self.get_config().get("place_prizes", {})
            full_list = self._podium_list(place_prizes)
            ranked_total = len([x for x in full_list if x["ranked"]])
            if self.status != "podium_step":
                self.status = "podium_step"
                self.podium_reveal_count = 1
                return {"ok": True, "status": self.status}

            if self.podium_reveal_count < ranked_total:
                self.podium_reveal_count += 1
            else:
                self.status = "finished"
            return {"ok": True, "status": self.status}

    async def show_full_podium_now(self) -> dict:
        async with self.lock:
            self.status = "finished"
            self.question_ends_at = None
            return {"ok": True, "status": self.status}

    async def reset_game(self) -> None:
        async with self.lock:
            self.status = "lobby"
            self.current_index = -1
            self.question_ends_at = None
            self.podium_reveal_count = 0
            for team in self.teams.values():
                team.score = 0
                team.votes.clear()
                team.submitted_option = None
                team.submitted_text = ""
                team.verdict = None
                team.awarded_points = 0
                team.curator_change_allowed = False
                team.question_answers.clear()
                team.question_verdicts.clear()

    def get_remaining_seconds(self) -> int:
        if self.status != "question" or not self.question_ends_at:
            return 0
        return max(0, int(round(self.question_ends_at - time.time())))

    def _podium_list(self, place_prizes: Dict[str, str]) -> List[dict]:
        order_ids = self.active_team_ids()
        active_teams = [self.teams[tid] for tid in order_ids]
        off_podium = self.is_organizers_off_podium()

        ranked_teams = [t for t in active_teams if not (off_podium and t.is_organizers)]
        unranked_teams = [t for t in active_teams if (off_podium and t.is_organizers)]

        sorted_ranked = sorted(ranked_teams, key=lambda t: (-t.score, order_ids.index(t.id)))
        result = []
        place = 0
        prev_score = None
        for idx, t in enumerate(sorted_ranked):
            if t.score != prev_score:
                place = idx + 1
                prev_score = t.score
            prize_text = place_prizes.get(str(place), "")
            result.append({
                "id": t.id,
                "name": t.name,
                "color": t.color,
                "score": t.score,
                "place": place,
                "prize": prize_text,
                "ranked": True,
            })

        for t in unranked_teams:
            result.append({
                "id": t.id,
                "name": t.name,
                "color": t.color,
                "score": t.score,
                "place": None,
                "prize": "",
                "ranked": False,
            })

        return result

    def build_broadcast_state(self) -> dict:
        self.sync_teams_from_config()
        cfg = self.get_config()
        cap = self.get_team_capacity()
        org_enabled = self.is_organizers_enabled()
        questions = cfg.get("questions", [])
        q = self.get_current_question()
        q_type = q.get("question_type", "choice") if q else "choice"
        correct = q.get("correct_option") if (q and q_type == "choice") else None
        place_prizes = cfg.get("place_prizes", {})

        questions_summary = [
            {
                "id": item.get("id", ""),
                "index": idx + 1,
                "question_type": item.get("question_type", "choice"),
                "text": item.get("text", ""),
                "correct_option": item.get("correct_option", ""),
                "reference_answer": item.get("reference_answer", ""),
                "points": item.get("points", 100),
                "partial_points": item.get("partial_points", 50),
            }
            for idx, item in enumerate(questions)
        ]

        teams_info = []
        for tid in self.active_team_ids():
            t = self.teams[tid]
            cur_name = "Не назначен"
            if t.curator_token and t.curator_token in t.members:
                cur_name = t.members[t.curator_token].nickname
            has_submitted = bool(t.submitted_text.strip()) if q_type == "open" else bool(t.submitted_option)
            is_correct = (t.verdict == "correct") or (
                q_type == "choice" and self.status in ("reveal", "finished") and t.submitted_option == correct
            )
            answers_by_q = {}
            verdicts_by_q = {}
            for item in questions:
                qid = item.get("id", "")
                if qid in t.question_answers:
                    answers_by_q[qid] = t.question_answers[qid]
                elif q and q.get("id") == qid:
                    answers_by_q[qid] = t.submitted_text.strip() if q_type == "open" else (t.submitted_option or self.get_leading_option(t, q))
                else:
                    answers_by_q[qid] = None
                verdicts_by_q[qid] = t.question_verdicts.get(qid)

            teams_info.append({
                "id": t.id,
                "name": t.name,
                "color": t.color,
                "is_organizers": t.is_organizers,
                "count": len(t.members),
                "capacity": cap,
                "curator": cur_name,
                "has_voted": len(t.votes) > 0 or has_submitted,
                "voted_count": len(t.votes),
                "submitted_option": t.submitted_option,
                "submitted_text": t.submitted_text,
                "has_submitted": has_submitted,
                "verdict": t.verdict,
                "awarded_points": t.awarded_points,
                "curator_change_allowed": t.curator_change_allowed,
                "score": t.score,
                "is_correct": is_correct,
                "answers_by_q": answers_by_q,
                "verdicts_by_q": verdicts_by_q,
            })

        full_podium = self._podium_list(place_prizes) if self.status in ("podium_step", "finished") else []
        ranked_only = [x for x in full_podium if x["ranked"]]
        unranked_only = [x for x in full_podium if not x["ranked"]]

        current_step_item = None
        revealed_from_bottom = []
        if self.status == "podium_step" and ranked_only:
            reversed_ranked = list(reversed(ranked_only))
            count = max(1, min(self.podium_reveal_count, len(reversed_ranked)))
            revealed_from_bottom = reversed_ranked[:count]
            current_step_item = revealed_from_bottom[-1]

        return {
            "status": self.status,
            "quiz_title": cfg.get("quiz_title", "КВИЗ"),
            "logo_url": cfg.get("logo_url", ""),
            "team_capacity": cap,
            "organizers_enabled": org_enabled,
            "organizers_off_podium": self.is_organizers_off_podium(),
            "auto_random_unanswered": self.is_auto_random_unanswered(),
            "all_submitted": self.all_active_teams_submitted(),
            "remaining_seconds": self.get_remaining_seconds(),
            "thinking_music_url": cfg.get("thinking_music_url", ""),
            "sponsor": {
                "enabled": cfg.get("sponsor_enabled", True),
                "title": cfg.get("sponsor_title", ""),
                "description": cfg.get("sponsor_description", ""),
                "prizes": cfg.get("sponsor_prizes", ""),
                "image_url": cfg.get("sponsor_image_url", ""),
            },
            "question_index": self.current_index + 1,
            "total_questions": len(questions),
            "questions_summary": questions_summary,
            "question": {
                "id": q.get("id", ""),
                "question_type": q_type,
                "text": q.get("text", ""),
                "options": q.get("options", []),
                "reference_answer": q.get("reference_answer", ""),
                "image_url": q.get("image_url", ""),
                "audio_url": q.get("audio_url", ""),
                "points": q.get("points", 100),
                "partial_points": q.get("partial_points", 50),
                "time_limit": q.get("time_limit", 30),
                "correct_option": correct if self.status in ("reveal", "finished") else None,
            }
            if q
            else None,
            "teams": teams_info,
            "podium": full_podium,
            "podium_step": {
                "current": current_step_item,
                "revealed": revealed_from_bottom,
                "unranked": unranked_only,
                "step_num": self.podium_reveal_count,
                "total_ranked": len(ranked_only),
            },
            "place_prizes": place_prizes,
        }

    def build_team_state(self, team_id: str) -> dict:
        self.sync_teams_from_config()
        cfg = self.get_config()
        cap = self.get_team_capacity()
        if team_id not in self.teams:
            return {"status": self.status, "team": None}
        t = self.teams[team_id]
        questions = cfg.get("questions", [])
        q = self.get_current_question()
        q_type = q.get("question_type", "choice") if q else "choice"
        cur_name = "Не назначен"
        if t.curator_token and t.curator_token in t.members:
            cur_name = t.members[t.curator_token].nickname

        return {
            "status": self.status,
            "quiz_title": cfg.get("quiz_title", "КВИЗ"),
            "remaining_seconds": self.get_remaining_seconds(),
            "organizers_enabled": self.is_organizers_enabled(),
            "team": {
                "id": t.id,
                "name": t.name,
                "color": t.color,
                "count": len(t.members),
                "capacity": cap,
                "score": t.score,
                "curator_token": t.curator_token,
                "curator_name": cur_name,
                "vote_counts": self.get_team_vote_counts(t, q),
                "leading_option": self.get_leading_option(t, q),
                "submitted_option": t.submitted_option,
                "submitted_text": t.submitted_text,
                "verdict": t.verdict,
                "curator_change_allowed": t.curator_change_allowed,
            },
            "question": {
                "id": q.get("id", ""),
                "index": self.current_index + 1,
                "total": len(questions),
                "question_type": q_type,
                "text": q.get("text", ""),
                "options": q.get("options", []),
                "reference_answer": q.get("reference_answer", "") if self.status in ("reveal", "finished") else "",
                "image_url": q.get("image_url", ""),
                "audio_url": q.get("audio_url", ""),
                "points": q.get("points", 100),
                "partial_points": q.get("partial_points", 50),
                "correct_option": q.get("correct_option") if (self.status in ("reveal", "finished") and q_type == "choice") else None,
            }
            if q
            else None,
        }
