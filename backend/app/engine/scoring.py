from dataclasses import dataclass

from app.core.enums import Direction
from app.strategies.base import StrategyResult


@dataclass(frozen=True)
class ScoreResult:
    direction: Direction
    score: int
    confidence_label: str
    reason: str
    breakdown: dict


_NEUTRAL_DIRECTIONS = {Direction.NEUTRAL}
_MIN_PARTICIPATION_RATIO = 0.35
_STRONG_SCORE_THRESHOLD = 75
_LABEL_THRESHOLDS = (
    (40, "debole"),
    (60, "medio"),
    (75, "buono"),
    (90, "forte"),
)
_LABEL_TOP = "molto forte"


class SignalScoringEngine:
    def __init__(
        self,
        participation_ratio: float = _MIN_PARTICIPATION_RATIO,
        *,
        strong_score_threshold: int = _STRONG_SCORE_THRESHOLD,
        label_thresholds: tuple[tuple[int, str], ...] = _LABEL_THRESHOLDS,
        label_top: str = _LABEL_TOP,
    ) -> None:
        self.participation_ratio = max(0.0, min(1.0, participation_ratio))
        self.strong_score_threshold = int(strong_score_threshold)
        self.label_thresholds = label_thresholds
        self.label_top = label_top

    def score(
        self,
        results: list[StrategyResult],
        weights: dict[str, int],
        *,
        participation_ratio: float | None = None,
    ) -> ScoreResult:
        effective_ratio = (
            self.participation_ratio
            if participation_ratio is None
            else max(0.0, min(1.0, participation_ratio))
        )
        long_score = 0.0
        short_score = 0.0
        all_weight = sum(max(0, weight) for weight in weights.values())
        voting_weight = 0
        breakdown: dict[str, dict] = {}
        reasons: list[str] = []
        for result in results:
            weight = weights.get(result.strategy_name, 10)
            contribution = (max(0, min(result.confidence, 100)) / 100.0) * weight
            if result.direction in {Direction.LONG, Direction.STRONG_LONG}:
                long_score += contribution
                voting_weight += max(0, weight)
            elif result.direction in {Direction.SHORT, Direction.STRONG_SHORT}:
                short_score += contribution
                voting_weight += max(0, weight)
            breakdown[result.strategy_name] = {
                "direction": result.direction.value,
                "confidence": result.confidence,
                "weight": weight,
                "contribution": round(contribution, 2),
                "reason": result.reason,
                "indicator_values": result.indicator_values,
            }
            if result.direction not in _NEUTRAL_DIRECTIONS:
                reasons.append(result.reason)

        # Normalizza sul peso delle sole strategie con posizione (non neutrali).
        # Se meno della soglia di participation del peso totale ha preso posizione, segnale troppo debole.
        min_participation = all_weight * effective_ratio
        if voting_weight < min_participation or voting_weight == 0:
            scale = 0.0
        else:
            scale = 100.0 / voting_weight

        normalized_long_score = round(long_score * scale)
        normalized_short_score = round(short_score * scale)

        if normalized_long_score == normalized_short_score:
            # Parity → segnale ambiguo: forziamo score=0 per coerenza con i filtri downstream
            # (`score < min_signal_score` ora distingue chiaramente "parity" da "weak score").
            direction = Direction.NEUTRAL
            score = 0
        elif normalized_long_score > normalized_short_score:
            score = min(100, normalized_long_score)
            direction = (
                Direction.STRONG_LONG
                if score >= self.strong_score_threshold
                else Direction.LONG
            )
        else:
            score = min(100, normalized_short_score)
            direction = (
                Direction.STRONG_SHORT
                if score >= self.strong_score_threshold
                else Direction.SHORT
            )
        breakdown["_score"] = {
            "long_raw": round(long_score, 2),
            "short_raw": round(short_score, 2),
            "all_weight": all_weight,
            "voting_weight": voting_weight,
            "long_normalized": normalized_long_score,
            "short_normalized": normalized_short_score,
        }
        return ScoreResult(
            direction=direction,
            score=score,
            confidence_label=self.confidence_label(score),
            reason="; ".join(reasons) if reasons else "No aligned strategy signal",
            breakdown=breakdown,
        )

    def confidence_label(self, score: int) -> str:
        for threshold, label in self.label_thresholds:
            if score < threshold:
                return label
        return self.label_top
