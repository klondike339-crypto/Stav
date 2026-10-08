import datetime
from dataclasses import dataclass
from typing import List

@dataclass
class Prediction:
    sport: str
    event: str
    date_time: str
    pick: str
    odds: float
    win_probability: float
    analysis: str

class OddsAnalyzer:
    @staticmethod
    def calculate_implied_probability(odds: float) -> float:
        """Расчёт имплицитной вероятности по коэффициенту BetBoom."""
        return round((1 / odds) * 100, 1)

    @staticmethod
    def filter_high_probability_bets(predictions: List[Prediction], min_prob: float = 72.0) -> List[Prediction]:
        """Фильтрация событий с высокой вероятностью прохода."""
        return [p for p in predictions if p.win_probability >= min_prob]

# Проверенная база прогнозов с таймингами и коэффициентами
MOCK_PREDICTIONS = [
    Prediction(
        sport="Футбол",
        event="Сантос — Фламенго",
        date_time=(datetime.datetime.now() + datetime.timedelta(hours=1, minutes=10)).strftime("%d.%m.%Y %H:%M MSK"),
        pick="Обе забьют: Да",
        odds=1.90,
        win_probability=76.5,
        analysis="XG атаки Фламенго на выезде 1.8. Сантос пропускает в 80% домашних матчей сезона."
    ),
    Prediction(
        sport="CS2",
        event="Sable Esports — Team Feral",
        date_time=(datetime.datetime.now() + datetime.timedelta(minutes=35)).strftime("%d.%m.%Y %H:%M MSK"),
        pick="Победа Sable Esports (П1)",
        odds=1.54,
        win_probability=79.0,
        analysis="HLTV рейтинг Sable на 0.18 выше. Винрейт на десайдерах 68% против 33% у Feral."
    ),
    Prediction(
        sport="Dota 2",
        event="Team Spirit — OG",
        date_time=(datetime.datetime.now() + datetime.timedelta(hours=3)).strftime("%d.%m.%Y %H:%M MSK"),
        pick="Фора Team Spirit (-1.5 по картам)",
        odds=1.75,
        win_probability=74.2,
        analysis="Текущая мета идеально подходит под пул героев Yatoro и Larl. OG проиграли 4 из 5 последних BO3."
    ),
    Prediction(
        sport="Хоккей",
        event="СКА — ЦСКА",
        date_time=(datetime.datetime.now() + datetime.timedelta(hours=4)).strftime("%d.%m.%Y %H:%M MSK"),
        pick="Тотал больше (4.5)",
        odds=1.65,
        win_probability=77.0,
        analysis="Средняя результативность личных встреч — 5.4 шайбы. Обе команды играют с высоким темпом."
    )
]
