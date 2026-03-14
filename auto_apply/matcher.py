"""CV-to-job matching using Claude Haiku."""

import json
import logging
import re
import time

import anthropic

from auto_apply.config import CLAUDE_MODEL_SCORING, SCORE_THRESHOLD
from auto_apply.models import MatchResult

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a job matching expert. You will be given a candidate's CV and a job posting.
Score how well the candidate matches the job on a scale of 0-100.

Scoring guide:
- 90-100: Near-perfect match. Candidate has almost all required skills and experience.
- 70-89: Strong match. Candidate has most required skills, some gaps are minor.
- 50-69: Partial match. Candidate has some relevant skills but significant gaps.
- 30-49: Weak match. Limited overlap between candidate skills and job requirements.
- 0-29: Poor match. Candidate is not suited for this role.

Consider:
1. Technical skills match (Python, SQL, cloud, ML, etc.)
2. Experience level match (years, seniority)
3. Domain relevance (data analytics, marketing tech, iGaming industry)
4. Tool/platform match (Power BI, GA4, Streamlit, D365, etc.)
5. Salary expectations vs candidate's level

You MUST respond with valid JSON only. No other text.

{
  "score": <int 0-100>,
  "reasoning": "<1-2 sentence explanation>",
  "matched_skills": ["skill1", "skill2"],
  "missing_skills": ["skill1", "skill2"]
}"""

_claude_client: anthropic.Anthropic | None = None


def _get_client(api_key: str) -> anthropic.Anthropic:
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=api_key)
    return _claude_client


def score_job(
    job_id: int,
    title: str,
    company: str,
    description: str,
    salary_text: str,
    cv_text: str,
    api_key: str,
) -> MatchResult:
    """Score a job against the CV using Claude Haiku.

    Args:
        job_id: Database ID of the job.
        title: Job title.
        company: Company name.
        description: Full job description text.
        salary_text: Human-readable salary string.
        cv_text: Extracted CV text (first 3000 chars used).
        api_key: Anthropic API key.

    Returns:
        MatchResult with score, reasoning, matched_skills, missing_skills.
    """
    user_msg = f"""## Candidate CV
{cv_text[:3000]}

## Job Posting
**Title:** {title}
**Company:** {company}
**Salary:** {salary_text or 'Not specified'}

**Description:**
{description[:3000]}

Score this match as JSON."""

    backoff = 2
    for attempt in range(3):
        try:
            client = _get_client(api_key)
            response = client.messages.create(
                model=CLAUDE_MODEL_SCORING,
                max_tokens=512,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            content = response.content[0].text

            # Parse JSON — handle markdown code fences
            json_str = content
            json_match = re.search(r'```(?:json)?\s*(.*?)```', content, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)

            data = json.loads(json_str.strip())
            return MatchResult(
                job_id=job_id,
                score=max(0, min(100, int(data.get("score", 0)))),
                reasoning=data.get("reasoning", ""),
                matched_skills=data.get("matched_skills", []),
                missing_skills=data.get("missing_skills", []),
            )

        except anthropic.RateLimitError:
            log.warning(f"Rate limited scoring job {job_id} — retrying in {backoff}s")
            time.sleep(backoff)
            backoff *= 2
            continue
        except anthropic.APIError as e:
            log.error(f"Claude API error scoring job {job_id} ({title}): {e}")
            return MatchResult(job_id=job_id, score=0, reasoning=f"Scoring failed: {e}")
        except Exception as e:
            log.error(f"Scoring failed for job {job_id} ({title}): {e}")
            return MatchResult(job_id=job_id, score=0, reasoning=f"Scoring failed: {e}")

    log.error(f"All retries exhausted for job {job_id} ({title})")
    return MatchResult(job_id=job_id, score=0, reasoning="Scoring failed: rate limit retries exhausted")


def score_jobs_batch(jobs: list[dict], api_key: str, cv_text: str) -> list[MatchResult]:
    """Score multiple jobs sequentially.

    Args:
        jobs: List of job dicts from the database.
        api_key: Anthropic API key.
        cv_text: Extracted CV text to score against.

    Returns:
        List of MatchResult objects.
    """
    results = []
    total = len(jobs)

    for i, job in enumerate(jobs, 1):
        log.info(f"Scoring [{i}/{total}]: {job['title']} at {job['company']}")
        result = score_job(
            job_id=job["id"],
            title=job["title"],
            company=job["company"],
            description=job.get("description", ""),
            salary_text=job.get("salary_text", ""),
            cv_text=cv_text,
            api_key=api_key,
        )
        log.info(f"  Score: {result.score}/100 — {result.reasoning[:80]}")
        results.append(result)

    above = sum(1 for r in results if r.score >= SCORE_THRESHOLD)
    log.info(f"Scoring complete: {above}/{total} jobs above threshold ({SCORE_THRESHOLD})")
    return results
