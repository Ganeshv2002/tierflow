from naukri_assistant.matching import score_candidate
from naukri_assistant.models import JobCandidate, ProfileSettings, ResumeProfile
from naukri_assistant.text_utils import parse_experience_ranges


def _resume() -> ResumeProfile:
    return ResumeProfile(source_path="resume.txt", raw_text="Software Engineer, Python", normalized_text="software engineer python",
                         skills=["python"], titles=["software engineer"], detected_experience_years=[2.0])


def _job(experience: str) -> JobCandidate:
    return JobCandidate(stable_key="k", url="https://example.test/k", title="Software Engineer", company="Acme",
                        snippet=f"Python. Experience: {experience}", raw_text=f"Python. Experience: {experience}",
                        experience_text=experience)


def test_word_ranges_parse():
    assert parse_experience_ranges("Experience: 1 to 3 years") == [(1.0, 3.0)]
    assert parse_experience_ranges("1-3 yrs") == [(1.0, 3.0)]
    assert parse_experience_ranges("5+ years") == [(5.0, 5.0)]


def test_word_range_counts_as_inside_target():
    score = score_candidate(_resume(), _job("1 to 3 years"), ProfileSettings(resume_path="resume.txt"))
    assert any("experience range includes 2 years" in reason for reason in score.reasons)
