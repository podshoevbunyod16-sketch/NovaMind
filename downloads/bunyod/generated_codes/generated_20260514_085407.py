# Импорт необходимых библиотек
from flask import Flask, render_template, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user, login_required
import os

# Инициализация Flask приложения
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///instagram.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Инициализация базы данных
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Модель пользователя
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(128))
    posts = db.relationship('Post', backref='author', lazy=True)

# Модель поста
class Post(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image_url = db.Column(db.String(300), nullable=False)
    caption = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

# Загрузчик пользователя для Flask-Login
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Главная страница (лента)
@app.route('/')
@login_required
def home():
    posts = Post.query.order_by(Post.id.desc()).all()
    return render_template('home.html', posts=posts)

# Страница регистрации
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        if User.query.filter_by(username=username).first():
            return 'Пользователь уже существует'
        
        new_user = User(username=username, password_hash=generate_password_hash(password))
        db.session.add(new_user)
        db.session.commit()
        
        return redirect(url_for('login'))
    return render_template('register.html')

# Страница входа
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = User.query.filter_by(username=username).first()
        
        if not user or not check_password_hash(user.password_hash, password):
            return 'Неверный логин или пароль'
        
        login_user(user)
        return redirect(url_for('home'))
    return render_template('login.html')

# Выход из системы
@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# Создание нового поста
@app.route('/create', methods=['GET', 'POST'])
@login_required
def create():
    if request.method == 'POST':
        image_url = request.form['image_url']
        caption = request.form['caption']
        
        new_post = Post(image_url=image_url, caption=caption, author=current_user)
        db.session.add(new_post)
        db.session.commit()
        
        return redirect(url_for('home'))
    return render_template('create.html')

# Запуск приложения
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)