import json
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, g, flash, jsonify
from datetime import datetime, timedelta
import sys
import re

app = Flask(__name__)
app.secret_key = "supersecretkey"
DATABASE = "scheduler.db"

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            day TEXT,
            time TEXT,
            course TEXT,
            venue TEXT,
            recurrence TEXT DEFAULT 'none',
            reminder_enabled INTEGER DEFAULT 0,
            reminder_minutes_before INTEGER DEFAULT 10,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        ''')
        db.commit()

def clean_time_str(time_str):
    # Ensure space before am/pm
    return re.sub(r'(\d)(am|pm|AM|PM)$', r'\1 \2', time_str)

def to_24hr(time_str):
    time_str = clean_time_str(time_str)
    try:
        return datetime.strptime(time_str, '%H:%M').strftime('%H:%M')
    except ValueError:
        try:
            return datetime.strptime(time_str, '%I:%M %p').strftime('%H:%M')
        except ValueError:
            return time_str  # fallback, may break calendar

def parse_event_time(date_str, time_str):
    time_str = clean_time_str(time_str)
    try:
        return datetime.strptime(date_str + ' ' + time_str, '%Y-%m-%d %H:%M')
    except ValueError:
        try:
            return datetime.strptime(date_str + ' ' + time_str, '%Y-%m-%d %I:%M %p')
        except ValueError as e:
            print('Time parse error:', e, file=sys.stderr)
            return None

def log_reminder_check(row, event_time, delta):
    print(f"Reminder check: course={row['course']}, time={row['time']}, event_time={event_time}, delta={delta}, enabled={row['reminder_enabled']}, window={row['reminder_minutes_before']}", file=sys.stderr)

@app.route("/")
def index():
    if "user_id" in session:
        user_id = session["user_id"]
        username = session["username"]
        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        today_day = now.strftime('%A')
        db = get_db()
        cur = db.execute("SELECT * FROM schedules WHERE user_id=?", (user_id,))
        today_events = []
        upcoming_reminders = []
        for row in cur.fetchall():
            if row['recurrence'] == 'none' and row['date'] == today_str:
                today_events.append(row)
            elif row['recurrence'] == 'weekly' and row['day'] == today_day:
                today_events.append(row)
            event_time = None
            if row['date'] == today_str and row['time']:
                event_time = parse_event_time(today_str, row['time'])
            elif row['recurrence'] == 'weekly' and row['day'] == today_day and row['time']:
                event_time = parse_event_time(today_str, row['time'])
            if event_time and row['reminder_enabled']:
                delta = (event_time - now).total_seconds() / 60
                log_reminder_check(row, event_time, delta)
                if 0 < delta <= row['reminder_minutes_before']:
                    upcoming_reminders.append({
                        'course': row['course'],
                        'time': row['time'],
                        'venue': row['venue'],
                        'minutes_left': int(delta)
                    })
        return render_template("dashboard.html", user=username, today=now.strftime('%A'), today_events=today_events, upcoming_reminders=upcoming_reminders)
    return redirect(url_for("login"))

# View all schedules for the logged-in user
@app.route("/view_all")
def view_all():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    username = session["username"]
    db = get_db()
    cur = db.execute("SELECT * FROM schedules WHERE user_id=? ORDER BY day, time", (user_id,))
    all_schedules = cur.fetchall()
    return render_template("view_all.html", user=username, schedules=all_schedules)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        db = get_db()
        try:
            db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            db.commit()
            flash("Registration successful! Please login.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        db = get_db()
        cur = db.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
        user = cur.fetchone()
        if user:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials.", "danger")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/add", methods=["GET", "POST"])
def add():
    if "user_id" not in session:
        return redirect(url_for("login"))
    # When saving recurring events, ensure day is a full name
    valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if request.method == "POST":
        recurrence = request.form["recurrence"]
        time = request.form["time"]
        course = request.form["course"]
        venue = request.form["venue"]
        user_id = session["user_id"]
        reminder_enabled = int(request.form.get('reminder_enabled', 0))
        reminder_minutes_before = int(request.form.get('reminder_minutes_before', 10))
        if recurrence == "none":
            date = request.form["date"]
            db = get_db()
            db.execute("INSERT INTO schedules (user_id, date, time, course, venue, recurrence, reminder_enabled, reminder_minutes_before) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (user_id, date, time, course, venue, recurrence, reminder_enabled, reminder_minutes_before))
        else:
            day = request.form["day"]
            if day not in valid_days:
                flash("Invalid day selected.", "danger")
                return redirect(url_for("add"))
            db = get_db()
            db.execute("INSERT INTO schedules (user_id, day, time, course, venue, recurrence, reminder_enabled, reminder_minutes_before) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (user_id, day, time, course, venue, recurrence, reminder_enabled, reminder_minutes_before))
        db.commit()
        return redirect(url_for("index"))
    return render_template("add.html")

@app.route("/edit/<int:schedule_id>", methods=["GET", "POST"])
def edit(schedule_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    cur = db.execute("SELECT * FROM schedules WHERE id=? AND user_id=?", (schedule_id, session["user_id"]))
    schedule = cur.fetchone()
    if not schedule:
        flash("Schedule not found.", "danger")
        return redirect(url_for("index"))
    # When saving recurring events, ensure day is a full name
    valid_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if request.method == "POST":
        recurrence = request.form["recurrence"]
        time = request.form["time"]
        course = request.form["course"]
        venue = request.form["venue"]
        reminder_enabled = int(request.form.get('reminder_enabled', 0))
        reminder_minutes_before = int(request.form.get('reminder_minutes_before', 10))
        if recurrence == "none":
            date = request.form["date"]
            db.execute("UPDATE schedules SET date=?, day=NULL, time=?, course=?, venue=?, recurrence=?, reminder_enabled=?, reminder_minutes_before=? WHERE id=?",
                       (date, time, course, venue, recurrence, reminder_enabled, reminder_minutes_before, schedule_id))
        else:
            day = request.form["day"]
            if day not in valid_days:
                flash("Invalid day selected.", "danger")
                return redirect(url_for("edit", schedule_id=schedule_id))
            db.execute("UPDATE schedules SET day=?, date=NULL, time=?, course=?, venue=?, recurrence=?, reminder_enabled=?, reminder_minutes_before=? WHERE id=?",
                       (day, time, course, venue, recurrence, reminder_enabled, reminder_minutes_before, schedule_id))
        db.commit()
        return redirect(url_for("index"))
    return render_template("edit.html", schedule=schedule)

@app.route("/delete/<int:schedule_id>")
def delete(schedule_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    db.execute("DELETE FROM schedules WHERE id=? AND user_id=?", (schedule_id, session["user_id"]))
    db.commit()
    return redirect(url_for("index"))

# Route to clear all schedules for the logged-in user
@app.route("/clear_all")
def clear_all():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    db = get_db()
    db.execute("DELETE FROM schedules WHERE user_id=?", (user_id,))
    db.commit()
    return redirect(url_for("view_all"))

@app.route("/calendar")
def calendar():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session["user_id"]
    username = session["username"]
    db = get_db()
    cur = db.execute("SELECT * FROM schedules WHERE user_id=?", (user_id,))
    schedules = cur.fetchall()
    import calendar as pycal
    from datetime import date, timedelta
    events = []
    today = date.today()
    def get_dates_for_day(day_name):
        day_num = list(pycal.day_name).index(day_name)
        first_day = today.replace(day=1)
        dates = []
        d = first_day
        while d.month == today.month:
            if d.weekday() == day_num:
                dates.append(d)
            d += timedelta(days=1)
        return dates
    for s in schedules:
        if s['recurrence'] == 'weekly' and s['day']:
            for d in get_dates_for_day(s['day']):
                events.append({
                    "title": f"{s['course']} @ {s['venue']}",
                    "start": d.strftime('%Y-%m-%d') + 'T' + to_24hr(s['time'])
                })
        elif s['recurrence'] == 'none' and s['date']:
            events.append({
                "title": f"{s['course']} @ {s['venue']}",
                "start": s['date'] + 'T' + to_24hr(s['time'])
            })
    return render_template("calendar.html", user=username, events=json.dumps(events))

@app.route("/profile")
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    cur = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],))
    user = cur.fetchone()
    return render_template("profile.html", user=user)

# Update email
@app.route("/update_profile", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    email = request.form["email"]
    db = get_db()
    db.execute("UPDATE users SET email=? WHERE id=?", (email, session["user_id"]))
    db.commit()
    flash("Email updated!", "success")
    return redirect(url_for("profile"))

# Change password
@app.route("/change_password", methods=["POST"])
def change_password():
    if "user_id" not in session:
        return redirect(url_for("login"))
    password = request.form["password"]
    db = get_db()
    db.execute("UPDATE users SET password=? WHERE id=?", (password, session["user_id"]))
    db.commit()
    flash("Password changed!", "success")
    return redirect(url_for("profile"))

@app.route('/dashboard')
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user_id = session.get('user_id')
    db = get_db()
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    today_day = now.strftime('%A')
    cursor = db.execute('SELECT * FROM schedules WHERE user_id=?', (user_id,))
    today_events = []
    upcoming_reminders = []
    for row in cursor.fetchall():
        if row['recurrence'] == 'none' and row['date'] == today_str:
            today_events.append(row)
        elif row['recurrence'] == 'weekly' and row['day'] == today_day:
            today_events.append(row)
        event_time = None
        if row['date'] == today_str and row['time']:
            event_time = parse_event_time(today_str, row['time'])
        elif row['recurrence'] == 'weekly' and row['day'] == today_day and row['time']:
            event_time = parse_event_time(today_str, row['time'])
        if event_time and row['reminder_enabled']:
            delta = (event_time - now).total_seconds() / 60
            log_reminder_check(row, event_time, delta)
            if 0 < delta <= row['reminder_minutes_before']:
                upcoming_reminders.append({
                    'course': row['course'],
                    'time': row['time'],
                    'venue': row['venue'],
                    'minutes_left': int(delta)
                })
    return render_template('dashboard.html', user=session['username'], today_events=today_events, upcoming_reminders=upcoming_reminders)

@app.route('/reminders')
def reminders():
    if "user_id" not in session:
        return jsonify([])
    user_id = session.get('user_id')
    db = get_db()
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    today_day = now.strftime('%A')
    cursor = db.execute('SELECT * FROM schedules WHERE user_id=?', (user_id,))
    upcoming_reminders = []
    for row in cursor.fetchall():
        event_time = None
        if row['date'] == today_str and row['time']:
            event_time = parse_event_time(today_str, row['time'])
        elif row['recurrence'] == 'weekly' and row['day'] == today_day and row['time']:
            event_time = parse_event_time(today_str, row['time'])
        if event_time and row['reminder_enabled']:
            delta = (event_time - now).total_seconds() / 60
            if 0 < delta <= row['reminder_minutes_before']:
                upcoming_reminders.append({
                    'course': row['course'],
                    'time': row['time'],
                    'venue': row['venue'],
                    'minutes_left': int(delta)
                })
    return jsonify(upcoming_reminders)

@app.route('/admin')
def admin_dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    cur = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],))
    user = cur.fetchone()
    if not user or not user['is_admin']:
        flash("Access denied: Admins only.", "danger")
        return redirect(url_for("dashboard"))
    # Analytics
    total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_schedules = db.execute("SELECT COUNT(*) FROM schedules").fetchone()[0]
    busiest_day = db.execute("SELECT day, COUNT(*) as cnt FROM schedules WHERE recurrence='weekly' GROUP BY day ORDER BY cnt DESC LIMIT 1").fetchone()
    # User management
    all_users = db.execute("SELECT id, username, is_admin FROM users").fetchall()
    return render_template("admin.html", user=user, total_users=total_users, total_schedules=total_schedules, busiest_day=busiest_day, all_users=all_users)

@app.route('/admin/promote/<int:user_id>')
def promote_user(user_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    cur = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],))
    user = cur.fetchone()
    if not user or not user['is_admin']:
        flash("Access denied: Admins only.", "danger")
        return redirect(url_for("dashboard"))
    db.execute("UPDATE users SET is_admin=1 WHERE id=?", (user_id,))
    db.commit()
    flash("User promoted to admin!", "success")
    return redirect(url_for("admin_dashboard"))

@app.route('/admin/demote/<int:user_id>')
def demote_user(user_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    db = get_db()
    cur = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],))
    user = cur.fetchone()
    if not user or not user['is_admin']:
        flash("Access denied: Admins only.", "danger")
        return redirect(url_for("dashboard"))
    db.execute("UPDATE users SET is_admin=0 WHERE id=?", (user_id,))
    db.commit()
    flash("User demoted from admin!", "success")
    return redirect(url_for("admin_dashboard"))

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
