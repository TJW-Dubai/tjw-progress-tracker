from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from datetime import date, datetime, timedelta
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'tjw-dev-secret')

database_url = os.environ.get('DATABASE_URL', 'sqlite:///tracker.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

COACHES = ['Shruti', 'Veena', 'Poushali', 'Anindya', 'Swati', 'Ajay']

DEFAULT_BASELINES = [
    {'activity': 'comments',     'label': 'Comments',     'value': 25, 'suggested': 25},
    {'activity': 'posts',        'label': 'Posts',        'value': 3,  'suggested': 3},
    {'activity': 'outreach',     'label': 'Outreach',     'value': 50, 'suggested': 50},
    {'activity': 'applications', 'label': 'Applications', 'value': 18, 'suggested': 18},
]


def get_week_start(d=None):
    if d is None:
        d = date.today()
    return d - timedelta(days=d.weekday())


def activity_status(actual, target):
    if actual is None:
        return 'secondary'
    if target == 0:
        return 'success'
    if actual >= target:
        return 'success'
    if actual >= target * 0.7:
        return 'warning'
    return 'danger'


class Baseline(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    activity = db.Column(db.String(50), unique=True, nullable=False)
    label = db.Column(db.String(100), nullable=False)
    value = db.Column(db.Integer, nullable=False)
    suggested = db.Column(db.Integer, nullable=False)


def get_baselines():
    rows = Baseline.query.order_by(Baseline.id).all()
    return {b.activity: b.value for b in rows}


def get_baseline_rows():
    return Baseline.query.order_by(Baseline.id).all()


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    coach_name = db.Column(db.String(50), nullable=False)
    start_date = db.Column(db.Date, default=date.today)
    active = db.Column(db.Boolean, default=True)
    module_1_done = db.Column(db.Boolean, default=False)
    module_2_done = db.Column(db.Boolean, default=False)
    module_3_done = db.Column(db.Boolean, default=False)
    logs = db.relationship('WeeklyLog', backref='client', lazy=True,
                           order_by='WeeklyLog.week_start.desc()')

    def current_week_log(self):
        week = get_week_start()
        return WeeklyLog.query.filter_by(client_id=self.id, week_start=week).first()


class WeeklyLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    week_start = db.Column(db.Date, nullable=False)
    comments = db.Column(db.Integer, default=0)
    posts = db.Column(db.Integer, default=0)
    outreach = db.Column(db.Integer, default=0)
    applications = db.Column(db.Integer, default=0)
    follow_ups = db.Column(db.Integer, default=0)
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (db.UniqueConstraint('client_id', 'week_start', name='uq_client_week'),)

    def follow_up_target(self):
        return self.applications * 2

    def get_statuses(self, baselines):
        return {
            'comments': activity_status(self.comments, baselines['comments']),
            'posts': activity_status(self.posts, baselines['posts']),
            'outreach': activity_status(self.outreach, baselines['outreach']),
            'applications': activity_status(self.applications, baselines['applications']),
            'follow_ups': activity_status(self.follow_ups, self.follow_up_target()),
        }


@app.route('/')
def index():
    hour = datetime.now().hour
    if hour < 12:
        greeting = 'morning'
    elif hour < 17:
        greeting = 'afternoon'
    else:
        greeting = 'evening'
    return render_template('index.html', coaches=COACHES, greeting=greeting)


@app.route('/coach/<name>')
def coach(name):
    if name not in COACHES:
        return redirect(url_for('index'))
    clients = Client.query.filter_by(coach_name=name, active=True).order_by(Client.name).all()
    week_start = get_week_start()
    week_end = week_start + timedelta(days=6)
    return render_template('coach.html',
                           coach_name=name,
                           clients=clients,
                           week_start=week_start,
                           week_end=week_end,
                           baselines=get_baselines())


@app.route('/coach/<name>/add_client', methods=['POST'])
def add_client(name):
    if name not in COACHES:
        return redirect(url_for('index'))
    client_name = request.form.get('client_name', '').strip()
    if client_name:
        client = Client(name=client_name, coach_name=name)
        db.session.add(client)
        db.session.commit()
        flash(f'Client "{client_name}" added successfully.', 'success')
    return redirect(url_for('coach', name=name))


@app.route('/client/<int:client_id>/log', methods=['GET', 'POST'])
def log_week(client_id):
    client = Client.query.get_or_404(client_id)
    week_start = get_week_start()
    log = WeeklyLog.query.filter_by(client_id=client_id, week_start=week_start).first()

    if request.method == 'POST':
        comments = int(request.form.get('comments') or 0)
        posts = int(request.form.get('posts') or 0)
        outreach = int(request.form.get('outreach') or 0)
        applications = int(request.form.get('applications') or 0)
        follow_ups = int(request.form.get('follow_ups') or 0)
        notes = request.form.get('notes', '')

        client.module_1_done = 'module_1' in request.form
        client.module_2_done = 'module_2' in request.form
        client.module_3_done = 'module_3' in request.form

        if log:
            log.comments = comments
            log.posts = posts
            log.outreach = outreach
            log.applications = applications
            log.follow_ups = follow_ups
            log.notes = notes
            log.updated_at = datetime.utcnow()
        else:
            log = WeeklyLog(
                client_id=client_id,
                week_start=week_start,
                comments=comments,
                posts=posts,
                outreach=outreach,
                applications=applications,
                follow_ups=follow_ups,
                notes=notes,
            )
            db.session.add(log)

        db.session.commit()
        flash(f'Progress saved for {client.name}.', 'success')
        return redirect(url_for('coach', name=client.coach_name))

    week_end = week_start + timedelta(days=6)
    return render_template('log.html',
                           client=client,
                           log=log,
                           week_start=week_start,
                           week_end=week_end,
                           baselines=get_baselines())


@app.route('/client/<int:client_id>/history')
def client_history(client_id):
    client = Client.query.get_or_404(client_id)
    logs = WeeklyLog.query.filter_by(client_id=client_id).order_by(WeeklyLog.week_start.desc()).all()
    return render_template('client_history.html',
                           client=client,
                           logs=logs,
                           baselines=get_baselines())


@app.route('/client/<int:client_id>/archive', methods=['POST'])
def archive_client(client_id):
    client = Client.query.get_or_404(client_id)
    coach_name = client.coach_name
    client.active = False
    db.session.commit()
    flash(f'Client "{client.name}" archived.', 'info')
    return redirect(url_for('coach', name=coach_name))


@app.route('/dashboard')
def dashboard():
    week_start = get_week_start()
    week_end = week_start + timedelta(days=6)
    baselines = get_baselines()

    coach_data = []
    for coach_name in COACHES:
        clients = Client.query.filter_by(coach_name=coach_name, active=True).order_by(Client.name).all()
        client_data = []
        for client in clients:
            log = client.current_week_log()
            client_data.append({
                'client': client,
                'log': log,
                'statuses': log.get_statuses(baselines) if log else None,
            })
        coach_data.append({
            'name': coach_name,
            'clients': client_data,
            'total': len(client_data),
            'logged': sum(1 for c in client_data if c['log'] is not None),
        })

    return render_template('dashboard.html',
                           coach_data=coach_data,
                           week_start=week_start,
                           week_end=week_end,
                           baselines=baselines)


@app.route('/settings', methods=['GET'])
def settings():
    rows = get_baseline_rows()
    return render_template('settings.html', baselines=rows)


@app.route('/settings', methods=['POST'])
def settings_save():
    rows = get_baseline_rows()
    for b in rows:
        new_val = request.form.get(b.activity, '').strip()
        if new_val.isdigit():
            b.value = int(new_val)
    db.session.commit()
    flash('Baselines updated.', 'success')
    return redirect(url_for('settings'))


with app.app_context():
    db.create_all()
    if Baseline.query.count() == 0:
        for b in DEFAULT_BASELINES:
            db.session.add(Baseline(**b))
        db.session.commit()

if __name__ == '__main__':
    app.run(debug=True)
