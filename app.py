import os
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from models import db, Expert, ItemReview, TextAnnotation
from load_data import load_all

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "expert-review-dev-key-2024")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///expert_reviews.db"
).replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

# Load data once at startup
POST_IDS, POSTS, COMMENTS, MODELS = [], {}, {}, []


def init_data():
    global POST_IDS, POSTS, COMMENTS, MODELS
    POST_IDS, POSTS, COMMENTS, MODELS = load_all()


# ── Auth ──────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            return render_template("login.html", error="Username required")
        expert = Expert.query.filter_by(username=username).first()
        if not expert:
            expert = Expert(username=username)
            db.session.add(expert)
            db.session.commit()
        session["expert_id"] = expert.id
        session["username"] = expert.username
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def get_expert():
    eid = session.get("expert_id")
    if not eid:
        return None
    return Expert.query.get(eid)


# ── Dashboard ─────────────────────────────────────────

@app.route("/")
def dashboard():
    expert = get_expert()
    if not expert:
        return redirect(url_for("login"))

    search = request.args.get("search", "").strip()
    model_filter = request.args.get("model", MODELS[0] if MODELS else "")

    # Build post list with progress
    posts_list = []
    for pid in POST_IDS:
        key = (pid, model_filter)
        post = POSTS.get(key)
        if not post:
            continue
        comment_data = COMMENTS.get(pid, {})

        if search and search.lower() not in (post["title"] or "").lower() and search.lower() not in pid.lower():
            continue

        # Count total reviewable items
        total_items = (
            len(post["advice"])
            + len(post["divergences"])
            + len(post["clinical_notes"])
        )

        # Count reviewed items
        reviewed = ItemReview.query.filter_by(
            expert_id=expert.id, post_id=pid, model_name=model_filter
        ).filter(ItemReview.verdict.isnot(None)).count()

        # Count text annotations
        annot_count = TextAnnotation.query.filter_by(
            expert_id=expert.id, post_id=pid, model_name=model_filter
        ).count()

        posts_list.append({
            "post_id": pid,
            "title": post["title"],
            "class_label": post["class_label"],
            "total_items": total_items,
            "reviewed": reviewed,
            "annotations": annot_count,
            "link": post["link"],
        })

    return render_template(
        "dashboard.html",
        posts=posts_list,
        models=MODELS,
        current_model=model_filter,
        search=search,
        username=session.get("username"),
    )


# ── Review Page ───────────────────────────────────────

@app.route("/review/<post_id>")
def review(post_id):
    expert = get_expert()
    if not expert:
        return redirect(url_for("login"))

    model_name = request.args.get("model", MODELS[0] if MODELS else "")
    key = (post_id, model_name)
    post = POSTS.get(key)
    if not post:
        return "Post not found", 404

    comment_data = COMMENTS.get(post_id, {})

    # Load existing reviews
    existing_reviews = {}
    for r in ItemReview.query.filter_by(expert_id=expert.id, post_id=post_id, model_name=model_name).all():
        existing_reviews[(r.section, r.item_index)] = {"verdict": r.verdict, "note": r.note}

    # Load existing annotations
    existing_annotations = []
    for a in TextAnnotation.query.filter_by(expert_id=expert.id, post_id=post_id, model_name=model_name).all():
        existing_annotations.append({
            "id": a.id,
            "section": a.section,
            "start": a.start_offset,
            "end": a.end_offset,
            "text": a.highlighted_text,
            "annotation": a.annotation_text,
            "verdict": a.verdict,
        })

    # Prev/next navigation
    try:
        idx = POST_IDS.index(post_id)
    except ValueError:
        idx = 0
    prev_id = POST_IDS[idx - 1] if idx > 0 else None
    next_id = POST_IDS[idx + 1] if idx < len(POST_IDS) - 1 else None

    return render_template(
        "review.html",
        post=post,
        comment_data=comment_data,
        existing_reviews=existing_reviews,
        existing_annotations=existing_annotations,
        models=MODELS,
        current_model=model_name,
        prev_id=prev_id,
        next_id=next_id,
        username=session.get("username"),
    )


# ── API: Save item reviews ───────────────────────────

@app.route("/api/review/<post_id>/save", methods=["POST"])
def save_reviews(post_id):
    expert = get_expert()
    if not expert:
        return jsonify({"error": "Not logged in"}), 401

    data = request.json
    model_name = data.get("model_name", "")
    reviews = data.get("reviews", [])

    for r in reviews:
        existing = ItemReview.query.filter_by(
            expert_id=expert.id,
            post_id=post_id,
            model_name=model_name,
            section=r["section"],
            item_index=r["item_index"],
        ).first()

        if existing:
            existing.verdict = r.get("verdict")
            existing.note = r.get("note", "")
        else:
            new_review = ItemReview(
                expert_id=expert.id,
                post_id=post_id,
                model_name=model_name,
                section=r["section"],
                item_index=r["item_index"],
                verdict=r.get("verdict"),
                note=r.get("note", ""),
            )
            db.session.add(new_review)

    db.session.commit()
    return jsonify({"ok": True})


# ── API: Save text annotation ────────────────────────

@app.route("/api/annotation/<post_id>/save", methods=["POST"])
def save_annotation(post_id):
    expert = get_expert()
    if not expert:
        return jsonify({"error": "Not logged in"}), 401

    data = request.json
    annot = TextAnnotation(
        expert_id=expert.id,
        post_id=post_id,
        model_name=data["model_name"],
        section=data["section"],
        start_offset=data["start"],
        end_offset=data["end"],
        highlighted_text=data["text"],
        annotation_text=data.get("annotation", ""),
        verdict=data.get("verdict"),
    )
    db.session.add(annot)
    db.session.commit()
    return jsonify({"ok": True, "id": annot.id})


@app.route("/api/annotation/<int:annot_id>/delete", methods=["POST"])
def delete_annotation(annot_id):
    expert = get_expert()
    if not expert:
        return jsonify({"error": "Not logged in"}), 401
    annot = TextAnnotation.query.get(annot_id)
    if annot and annot.expert_id == expert.id:
        db.session.delete(annot)
        db.session.commit()
    return jsonify({"ok": True})


# ── Startup ───────────────────────────────────────────

with app.app_context():
    db.create_all()
    init_data()

if __name__ == "__main__":
    app.run(debug=True, port=5001, use_reloader=False)
