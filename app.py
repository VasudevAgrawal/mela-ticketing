from flask import (Flask, render_template, redirect, url_for, flash,
                   request, send_file, jsonify, Response)
from config import Config
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode
import io
import base64
import datetime
from werkzeug.utils import secure_filename
from pathlib import Path
import pandas as pd
import razorpay
import os

app = Flask(__name__)
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'images')
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

app.config.from_object(Config)

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

# Razorpay client (initialize if keys present)
if app.config.get('RAZORPAY_KEY_ID') and app.config.get('RAZORPAY_KEY_SECRET'):
    razorpay_client = razorpay.Client(auth=(app.config['RAZORPAY_KEY_ID'], app.config['RAZORPAY_KEY_SECRET']))
else:
    razorpay_client = None

# ----- Models -----
class Admin(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(300), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Ride(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    description = db.Column(db.Text)
    image = db.Column(db.String(300))
    capacity = db.Column(db.Integer, default=100)

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ride_id = db.Column(db.Integer, db.ForeignKey('ride.id'))
    ride = db.relationship('Ride')
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30))
    email = db.Column(db.String(150))
    qty = db.Column(db.Integer, default=1)
    total_amount = db.Column(db.Integer)
    status = db.Column(db.String(50), default='booked') # booked, paid, used, cancelled
    qr_data = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    razorpay_order_id = db.Column(db.String(200))
    razorpay_payment_id = db.Column(db.String(200))

# Create DB tables if they don't exist
with app.app_context():
    db.create_all()
    # create a default admin if none exists
    if not Admin.query.first():
        admin = Admin(username='admin')
        admin.set_password('admin123')
        db.session.add(admin)
        db.session.commit()

@login_manager.user_loader
def load_user(user_id):
    return Admin.query.get(int(user_id))

# ----- Utilities -----
def generate_qr_base64(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    img_bytes = buf.read()
    b64 = base64.b64encode(img_bytes).decode('utf-8')
    return f"data:image/png;base64,{b64}"

# ----- Routes -----
@app.route('/')
def index():
    rides = Ride.query.all()
    return render_template('index.html', rides=rides)

@app.route('/ride/<int:ride_id>')
def ride_detail(ride_id):
    ride = Ride.query.get_or_404(ride_id)
    return render_template('ride_detail.html', ride=ride)

@app.route('/book/<int:ride_id>', methods=['GET','POST'])
def book_ride(ride_id):
    ride = Ride.query.get_or_404(ride_id)
    if request.method == 'POST':
        name = request.form['name']
        phone = request.form.get('phone')
        email = request.form.get('email')
        qty = int(request.form.get('qty',1))
        total = ride.price * qty

        booking = Booking(ride=ride, name=name, phone=phone, email=email, qty=qty, total_amount=total)
        db.session.add(booking)
        db.session.commit()

        # create QR content: booking id + secret
        qr_payload = f"BOOKING:{booking.id}:{int(booking.created_at.timestamp())}"
        booking.qr_data = qr_payload
        db.session.commit()

        # If razorpay configured, create order
        if razorpay_client:
            order_amount = total * 100  # rupees to paise
            order_currency = 'INR'
            order_receipt = f'order_rcptid_{booking.id}'
            order = razorpay_client.order.create(dict(amount=order_amount, currency=order_currency, receipt=order_receipt, payment_capture=1))
            booking.razorpay_order_id = order.get('id')
            db.session.commit()
            return render_template('pay.html', booking=booking, razorpay_key=app.config.get('RAZORPAY_KEY_ID'))

        # else show ticket page with QR
        qr_img = generate_qr_base64(booking.qr_data)
        return render_template('ticket.html', booking=booking, qr_img=qr_img)

    return render_template('booking.html', ride=ride)

# Razorpay payment verification webhook (simple version)
@app.route('/payment/success', methods=['POST'])
def payment_success():
    booking_id = request.form.get('booking_id')
    payment_id = request.form.get('razorpay_payment_id')
    order_id = request.form.get('razorpay_order_id')

    booking = Booking.query.get(int(booking_id))

    if not booking:
        return "Booking Not Found", 404

    booking.razorpay_payment_id = payment_id
    booking.razorpay_order_id = order_id
    booking.status = "paid"
    db.session.commit()

    qr_img = generate_qr_base64(booking.qr_data)
    return render_template("ticket.html", booking=booking, qr_img=qr_img)


# Ticket display route (by id)
@app.route('/ticket/<int:booking_id>')
def show_ticket(booking_id):
    booking = Booking.query.get_or_404(booking_id)
    qr_img = generate_qr_base64(booking.qr_data)
    return render_template('ticket.html', booking=booking, qr_img=qr_img)

# Admin routes
@app.route('/admin/login', methods=['GET','POST'])
def admin_login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        admin = Admin.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            login_user(admin)
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))

@app.route('/admin')
@login_required
def admin_dashboard():
    total_bookings = Booking.query.count()
    total_revenue = db.session.query(db.func.sum(Booking.total_amount)).filter(Booking.status.in_(['paid','used'])).scalar() or 0
    rides = Ride.query.all()
    # prepare chart data: bookings per day for last 7 days
    today = datetime.date.today()
    days = [(today - datetime.timedelta(days=i)) for i in range(6,-1,-1)]
    labels = [d.strftime('%Y-%m-%d') for d in days]
    counts = []
    for d in days:
        start = datetime.datetime.combine(d, datetime.time.min)
        end = datetime.datetime.combine(d, datetime.time.max)
        c = Booking.query.filter(Booking.created_at >= start, Booking.created_at <= end).count()
        counts.append(c)
    return render_template('admin_dashboard.html', total_bookings=total_bookings, total_revenue=total_revenue, labels=labels, counts=counts, rides=rides)

@app.route('/admin/add_ride', methods=['GET','POST'])
@login_required
def add_ride():
    if request.method == 'POST':
        name = request.form['name']
        price = int(request.form['price'])
        desc = request.form.get('description')
        capacity = int(request.form.get('capacity',100))

        file = request.files.get('image_file')
        image_url = None

        if file and file.filename:
            fname = secure_filename(file.filename)
            ext = fname.rsplit('.', 1)[-1].lower()
            if ext in ALLOWED_EXT:
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], fname)
                file.save(save_path)
                image_url = url_for('static', filename=f"images/{fname}")

        ride = Ride(
            name=name,
            price=price,
            description=desc,
            image=image_url,
            capacity=capacity
        )

        db.session.add(ride)
        db.session.commit()
        flash('Ride added successfully!', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('add_ride.html')


@app.route('/admin/bookings')
@login_required
def admin_bookings():
    bookings = Booking.query.order_by(Booking.created_at.desc()).all()
    return render_template('bookings.html', bookings=bookings)

@app.route('/admin/delete_ride/<int:ride_id>')
@login_required
def delete_ride(ride_id):
    ride = Ride.query.get_or_404(ride_id)

    # Delete all bookings linked to this ride
    Booking.query.filter_by(ride_id=ride.id).delete()

    db.session.delete(ride)
    db.session.commit()

    flash("Ride deleted successfully!", "success")
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/export_csv')
@login_required
def export_csv():
    bookings = Booking.query.all()
    rows = []
    for b in bookings:
        rows.append({
            'booking_id': b.id,
            'ride': b.ride.name if b.ride else '',
            'name': b.name,
            'phone': b.phone,
            'email': b.email,
            'qty': b.qty,
            'total': b.total_amount,
            'status': b.status,
            'created_at': b.created_at,
        })
    df = pd.DataFrame(rows)
    csv = df.to_csv(index=False)
    return Response(csv, mimetype='text/csv', headers={'Content-Disposition':'attachment;filename=bookings.csv'})

@app.route('/admin/validate', methods=['GET','POST'])
@login_required
def validate_ticket():
    if request.method == 'POST':
        token = request.form.get('token')

        # token may be BOOKING:id:ts or plain ID
        bid = None
        if token.startswith('BOOKING:'):
            parts = token.split(':')
            try:
                bid = int(parts[1])
            except:
                bid = None
        else:
            try:
                bid = int(token)
            except:
                bid = None

        if not bid:
            flash('Invalid QR or ID', 'danger')
            return redirect(url_for('validate_ticket'))

        booking = Booking.query.get(bid)

        if not booking:
            flash('Booking not found', 'danger')
            return redirect(url_for('validate_ticket'))

        if booking.status == 'used':
            flash(f'Ticket #{booking.id} already used!', 'warning')
            return redirect(url_for('validate_ticket'))

        # ✅ Mark as used
        booking.status = 'used'
        db.session.commit()

        flash(f'Ticket #{booking.id} validated successfully ✅', 'success')
        return redirect(url_for('validate_ticket'))

    return render_template('scan_ticket.html')


# Simple API to fetch booking by QR payload (can be used by JS scanner)
@app.route('/api/booking_by_qr')
def api_booking_by_qr():
    payload = request.args.get('payload')
    if not payload:
        return jsonify({'error':'no payload'}), 400
    if payload.startswith('BOOKING:'):
        parts = payload.split(':')
        bid = int(parts[1])
        booking = Booking.query.get(bid)
        if booking:
            return jsonify({'id':booking.id,'ride':booking.ride.name,'status':booking.status})
    return jsonify({'error':'not found'}), 404

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, port=port)