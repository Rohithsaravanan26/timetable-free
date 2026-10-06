from datetime import datetime
from typing import List, Dict, Optional
import re
from collections import defaultdict
import os

# Load .env for local development (no-op on Vercel)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    ForeignKey, func, DateTime, Text
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship

# ======================
# CONFIG
# ======================

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL env var is not set. Add your Supabase PostgreSQL URL.")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

app = FastAPI(title="Timetable Predictor API – Free & Open")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ======================
# DB MODELS
# ======================

class Faculty(Base):
    __tablename__ = "faculties"
    id   = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    reviews = relationship("Review", back_populates="faculty")


class Review(Base):
    __tablename__ = "reviews"
    id         = Column(Integer, primary_key=True, index=True)
    faculty_id = Column(Integer, ForeignKey("faculties.id"))
    rating     = Column(Float)
    comment    = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    course_code  = Column(String, nullable=True)
    course_title = Column(String, nullable=True)
    faculty = relationship("Faculty", back_populates="reviews")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ======================
# TIMETABLE CONSTANTS
# ======================

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

PERIODS = {
    ("08:00", "09:00"): "P1",
    ("09:00", "10:00"): "P1",
    ("10:00", "11:00"): "P2",
    ("11:00", "12:00"): "P2",
    ("13:00", "14:00"): "P3",
    ("14:00", "15:00"): "P3",
    ("15:00", "16:00"): "P4",
    ("16:00", "17:00"): "P4",
    ("17:00", "18:00"): "P4",
    ("18:00", "19:00"): "P4",
}

# ======================
# PYDANTIC MODELS
# ======================

class Section(BaseModel):
    section_code: str
    course_name: str
    faculty_name: str
    time_slots: Dict[str, List[str]]
    faculty_rating: Optional[float] = None


class Timetable(BaseModel):
    sections: List[Section]


class Preferences(BaseModel):
    dislike_early:     bool = False
    dislike_midmorning: bool = False
    dislike_afternoon: bool = False
    dislike_evening:   bool = False
    prefer_weekend_off: bool = True
    preferred_faculty: List[str] = []
    avoid_faculty:     List[str] = []
    faculty_weight:    float = 0.6
    free_days_weight:  float = 0.2
    timing_weight:     float = 0.2


class GenerateRequest(BaseModel):
    raw_text: str
    chosen_courses: List[str]
    preferences: Preferences = Preferences()
    top_k: int = 5


class CoursesRequest(BaseModel):
    raw_text: str


class SectionSummary(BaseModel):
    section_code: str
    course_name: str
    faculty_name: str


class CourseSummary(BaseModel):
    course_name: str
    sections: List[SectionSummary]


class GenerateResponseItem(BaseModel):
    score: float
    timetable: Timetable
    grid: Dict[str, Dict[str, List[Dict[str, str]]]]


class ReviewIn(BaseModel):
    faculty_name: str
    rating: float
    comment: Optional[str] = None
    course_code: Optional[str] = None
    course_title: Optional[str] = None


class ReviewOut(BaseModel):
    rating: float
    comment: Optional[str]
    created_at: datetime
    course_code: Optional[str] = None
    course_title: Optional[str] = None


class FacultySummaryOut(BaseModel):
    faculty_name: str
    avg_rating: float
    count: int
    summary: str
    breakdown: Dict[int, int]
    reviews: List[ReviewOut]

# ======================
# TIMETABLE PARSER
# ======================

def normalize_faculty_name(name: str) -> str:
    return name.strip().upper()


def parse_sections(raw_text: str) -> List[Section]:
    # -------------------------
    # Preprocess
    # -------------------------
    def clean(line: str) -> str:
        line = line.strip()
        # Fix merged time ranges: 11:0012:00 → 11:00 12:00
        line = re.sub(r"(\d{2}:\d{2})(\d{2}:\d{2})", r"\1 \2", line)
        return line

    lines = [clean(l) for l in raw_text.splitlines() if clean(l)]

    sections: List[Section] = []

    current_course: Optional[str] = None
    pending_code: Optional[str] = None
    current_section = None
    time_slots = None

    NOISE_SUBSTRINGS = [
        "courses offered", "all enrolled", "not enrolled", "type a subject",
        "course overview", "full registration", "no. of attempts", "registration",
        "date:", "no. of attempt", "credits]"
    ]

    def is_noise_line(line: str) -> bool:
        low = line.lower()
        if any(ns in low for ns in NOISE_SUBSTRINGS):
            return True
        if any(line.startswith(d) for d in DAYS):
            return True
        if re.search(r"\d{2}:\d{2}", line):
            return True
        return False

    def looks_like_section(line: str) -> bool:
        return line.startswith(("UG -", "PG -")) and "," in line and "-" in line

    def looks_like_fallback_course_name(line: str) -> bool:
        if looks_like_section(line):
            return False
        if is_noise_line(line):
            return False
        if line.isupper() and "-" in line:
            return False
        return len(line.split()) >= 2

    CODE_RE = re.compile(r"^([0-9]{2}[A-Z]{2,}[0-9]{3})\s*\[.*?Credits?\]", re.I)
    TIME_RE = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")

    def flush():
        nonlocal current_section, time_slots
        if current_course and current_section:
            if any(time_slots[d] for d in DAYS):
                sections.append(
                    Section(
                        section_code=current_section["code"],
                        course_name=current_course,
                        faculty_name=current_section["faculty"],
                        time_slots=time_slots,
                        faculty_rating=None,
                    )
                )
        current_section = None
        time_slots = None

    # -------------------------
    # Main parse loop
    # -------------------------
    i = 0
    while i < len(lines):
        line = lines[i]

        # 1. Course code line (e.g. 19ME533 [4 Credits])
        code_match = CODE_RE.match(line)
        if code_match:
            pending_code = code_match.group(1).upper()
            i += 1
            continue

        # 2. Portal 'Course overview' followed by course name
        if line.lower() == "course overview" and i + 1 < len(lines):
            flush()
            course_title = lines[i + 1].strip()
            if pending_code:
                current_course = f"{pending_code} - {course_title}"
            else:
                current_course = course_title
            pending_code = None
            i += 2
            continue

        # 3. Section header
        if looks_like_section(line):
            flush()
            parts = [p.strip() for p in line.split(",")]
            section_code = parts[1] if len(parts) > 1 else parts[0]
            faculty = line.split("-")[-1].strip()
            current_section = {
                "code": section_code,
                "faculty": faculty,
            }
            time_slots = {d: [] for d in DAYS}
            i += 1
            continue

        # 4. Day + Time lines
        if current_section:
            for day in DAYS:
                if line.startswith(day):
                    ranges = TIME_RE.findall(line)
                    periods_seen = set()
                    for start, end in ranges:
                        if (start, end) in PERIODS:
                            periods_seen.add(PERIODS[(start, end)])
                    for p in periods_seen:
                        if p not in time_slots[day]:
                            time_slots[day].append(p)
                    break

        # 5. Fallback course name for standard/simple timetable format
        if looks_like_fallback_course_name(line):
            flush()
            current_course = line
            i += 1
            continue

        i += 1

    flush()
    return sections


# ======================
# SCORING HELPERS
# ======================

def clashes_with_current(current_sections: List[Section], new_section: Section) -> bool:
    occupied = set()
    for s in current_sections:
        for day, periods in s.time_slots.items():
            for p in periods:
                occupied.add((day, p))
    for day, periods in new_section.time_slots.items():
        for p in periods:
            if (day, p) in occupied:
                return True
    return False


def occupied_slots(sections: List[Section]) -> Dict[tuple, Section]:
    occ: Dict[tuple, Section] = {}
    for s in sections:
        for day, periods in s.time_slots.items():
            for p in periods:
                key = (day, p)
                if key in occ:
                    raise ValueError("Clash detected")
                occ[key] = s
    return occ


def free_days_score(occ: Dict[tuple, Section]) -> float:
    score = 0.0
    for day in DAYS:
        if not any(k[0] == day for k in occ.keys()):
            score += 1.0
            if day == "Saturday":
                score += 1.0
    return score


def timing_penalty(occ: Dict[tuple, Section], prefs: Preferences) -> float:
    penalty = 0.0
    for (_day, period) in occ.keys():
        if prefs.dislike_early and period == "P1":
            penalty -= 1.0
        if prefs.dislike_midmorning and period == "P2":
            penalty -= 1.0
        if prefs.dislike_afternoon and period == "P3":
            penalty -= 1.0
        if prefs.dislike_evening and period == "P4":
            penalty -= 1.0
    return penalty


def get_faculty_rating_db(db: Session, faculty_name: str) -> float:
    faculty_name = normalize_faculty_name(faculty_name)
    faculty = db.query(Faculty).filter(Faculty.name == faculty_name).first()
    if not faculty:
        return 3.5
    avg = db.query(func.avg(Review.rating)).filter(Review.faculty_id == faculty.id).scalar()
    return float(avg) if avg is not None else 3.5


def faculty_preference_score(sections: List[Section], prefs: Preferences) -> float:
    score = 0.0
    for sec in sections:
        if sec.faculty_name in prefs.preferred_faculty:
            score += 2.0
        if sec.faculty_name in prefs.avoid_faculty:
            score -= 3.0
    return score


def score_timetable(sections: List[Section], prefs: Preferences) -> float:
    try:
        occ = occupied_slots(sections)
    except ValueError:
        return -1e9

    ratings = [s.faculty_rating or 3.5 for s in sections]
    faculty_score = sum(ratings) / len(ratings) if ratings else 0.0
    free_score = free_days_score(occ) if prefs.prefer_weekend_off else 0.0
    time_pen = timing_penalty(occ, prefs)
    faculty_pref = faculty_preference_score(sections, prefs)

    return (
        prefs.faculty_weight * faculty_score
        + prefs.free_days_weight * free_score
        + prefs.timing_weight * time_pen
        + faculty_pref
    )


def group_by_course(sections: List[Section]) -> Dict[str, List[Section]]:
    by_course: Dict[str, List[Section]] = defaultdict(list)
    for s in sections:
        by_course[s.course_name].append(s)
    return by_course


def build_grid(sections: List[Section]) -> Dict[str, Dict[str, List[Dict[str, str]]]]:
    grid = {day: {p: [] for p in ["P1", "P2", "P3", "P4"]} for day in DAYS}
    for sec in sections:
        for day, periods in sec.time_slots.items():
            for p in periods:
                grid[day][p].append({
                    "course": sec.course_name,
                    "faculty": sec.faculty_name,
                    "section": sec.section_code,
                })
    return grid


def build_best_timetables(
    sections: List[Section],
    chosen_courses: List[str],
    prefs: Preferences,
    top_k: int = 5,
) -> List[GenerateResponseItem]:
    by_course = group_by_course(sections)
    filtered_courses = [c for c in chosen_courses if c in by_course]
    best: List[tuple] = []

    def backtrack(i: int, current_sections: List[Section]):
        nonlocal best
        if i == len(filtered_courses):
            sc = score_timetable(current_sections, prefs)
            best.append((sc, list(current_sections)))
            best.sort(key=lambda x: x[0], reverse=True)
            if len(best) > top_k:
                best[:] = best[:top_k]
            return
        course = filtered_courses[i]
        for sec in by_course[course]:
            if clashes_with_current(current_sections, sec):
                continue
            current_sections.append(sec)
            backtrack(i + 1, current_sections)
            current_sections.pop()

    backtrack(0, [])

    results: List[GenerateResponseItem] = []
    for score, secs in best:
        results.append(GenerateResponseItem(
            score=score,
            timetable=Timetable(sections=secs),
            grid=build_grid(secs),
        ))
    return results


# ======================
# FACULTY REVIEW HELPERS
# ======================

def build_faculty_summary(faculty_name: str, reviews: List[Review]) -> FacultySummaryOut:
    if not reviews:
        return FacultySummaryOut(
            faculty_name=faculty_name,
            avg_rating=0.0,
            count=0,
            summary="No student reviews yet. Be the first to share your experience.",
            breakdown={i: 0 for i in range(1, 6)},
            reviews=[],
        )

    ratings = [int(round(r.rating)) for r in reviews]
    avg = sum(ratings) / len(ratings)
    breakdown = {i: 0 for i in range(1, 6)}
    for r in ratings:
        if 1 <= r <= 5:
            breakdown[r] += 1

    if avg >= 4.5:
        tone = "Students consistently rate this faculty as excellent."
    elif avg >= 4.0:
        tone = "Students generally have a very good experience with this faculty."
    elif avg >= 3.0:
        tone = "Feedback is mixed — some students are satisfied, others see room for improvement."
    elif avg > 0:
        tone = "Students often find this faculty challenging, with several critical comments."
    else:
        tone = "No clear trend from reviews yet."

    out_reviews = [
        ReviewOut(
            rating=r.rating,
            comment=r.comment,
            created_at=r.created_at,
            course_code=r.course_code,
            course_title=r.course_title,
        )
        for r in sorted(reviews, key=lambda x: x.created_at, reverse=True)
    ]

    return FacultySummaryOut(
        faculty_name=faculty_name,
        avg_rating=avg,
        count=len(reviews),
        summary=tone,
        breakdown=breakdown,
        reviews=out_reviews,
    )


# ======================
# API ENDPOINTS
# ======================

@app.get("/", response_class=HTMLResponse)
def serve_index():
    index_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public", "index.html")
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Timetable Predictor API</h1>")


@app.get("/health")
@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/courses", response_model=List[CourseSummary])
@app.post("/api/courses", response_model=List[CourseSummary])
def get_courses(req: CoursesRequest):
    sections = parse_sections(req.raw_text)
    by_course = group_by_course(sections)
    return [
        CourseSummary(
            course_name=cn,
            sections=[
                SectionSummary(
                    section_code=s.section_code,
                    course_name=s.course_name,
                    faculty_name=s.faculty_name,
                )
                for s in secs
            ],
        )
        for cn, secs in by_course.items()
    ]


@app.post("/generate", response_model=List[GenerateResponseItem])
@app.post("/api/generate", response_model=List[GenerateResponseItem])
def generate(req: GenerateRequest, db: Session = Depends(get_db)):
    """Generate ranked, clash-free timetable options. Completely free, no login required."""
    sections = parse_sections(req.raw_text)
    for s in sections:
        s.faculty_rating = get_faculty_rating_db(db, s.faculty_name)

    results = build_best_timetables(sections, req.chosen_courses, req.preferences, req.top_k)

    if not results:
        raise HTTPException(
            status_code=400,
            detail="No valid timetable found without clashes. Try changing preferences or courses.",
        )
    return results


@app.get("/faculty/search")
@app.get("/api/faculty/search")
def search_faculty(q: str, db: Session = Depends(get_db)):
    if not q or len(q.strip()) < 2:
        return []
    q_upper = q.strip().upper()
    faculties = (
        db.query(Faculty)
        .filter(Faculty.name.contains(q_upper))
        .order_by(Faculty.name)
        .limit(10)
        .all()
    )
    return [{"id": f.id, "name": f.name} for f in faculties]


@app.get("/faculty/{faculty_id}/courses")
@app.get("/api/faculty/{faculty_id}/courses")
def get_faculty_courses(faculty_id: int, db: Session = Depends(get_db)):
    courses = (
        db.query(Review.course_code, Review.course_title)
        .filter(Review.faculty_id == faculty_id, Review.course_code.isnot(None))
        .distinct()
        .all()
    )
    return [{"course_code": c.course_code, "course_title": c.course_title} for c in courses]


@app.post("/review")
@app.post("/api/review")
def submit_review(review: ReviewIn, db: Session = Depends(get_db)):
    """Anonymous faculty review — no login required."""
    if review.rating < 1 or review.rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")

    faculty_name = normalize_faculty_name(review.faculty_name)
    faculty = db.query(Faculty).filter(Faculty.name == faculty_name).first()
    if not faculty:
        faculty = Faculty(name=faculty_name)
        db.add(faculty)
        db.commit()
        db.refresh(faculty)

    db.add(Review(
        faculty_id=faculty.id,
        rating=review.rating,
        comment=review.comment,
        course_code=review.course_code,
        course_title=review.course_title,
    ))
    db.commit()

    avg = get_faculty_rating_db(db, faculty.name)
    return {
        "message": "Review recorded (anonymous)",
        "faculty_name": faculty.name,
        "avg_rating": avg,
    }


@app.get("/faculty/{faculty_name}/reviews", response_model=FacultySummaryOut)
@app.get("/api/faculty/{faculty_name}/reviews", response_model=FacultySummaryOut)
def get_faculty_reviews(faculty_name: str, db: Session = Depends(get_db)):
    faculty_name = normalize_faculty_name(faculty_name)
    faculty = db.query(Faculty).filter(Faculty.name == faculty_name).first()
    if not faculty:
        return FacultySummaryOut(
            faculty_name=faculty_name,
            avg_rating=0.0,
            count=0,
            summary="No student reviews yet for this faculty.",
            breakdown={i: 0 for i in range(1, 6)},
            reviews=[],
        )
    reviews = db.query(Review).filter(Review.faculty_id == faculty.id).all()
    return build_faculty_summary(faculty_name, reviews)
