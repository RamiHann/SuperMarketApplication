import os
import uuid
import sqlite3
import json
from flask import Flask, render_template, request, redirect, url_for, session, g, send_from_directory
from werkzeug.utils import secure_filename
from PIL import Image  # For image conversion
import glob  # at the top if not already imported
import whisper
model = whisper.load_model("base")  # You can change "base" to "small", "medium", or "large"

app = Flask(__name__)
app.secret_key = "YOUR_SECRET_KEY"
DATABASE = "supermarket.db"

# Folder for uploaded images
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Create a folder for recordings
RECORDS_FOLDER = os.path.join("records")
os.makedirs(RECORDS_FOLDER, exist_ok=True)


# ------------------------------------------------------------------------------
# Database Helpers
# ------------------------------------------------------------------------------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    cursor = db.cursor()

    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')

    # Categories table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            category_id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_name TEXT NOT NULL,
            image_path TEXT
        )
    ''')

    # Subcategories table (each as a product with a price)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subcategories (
            subcategory_id INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id INTEGER NOT NULL,
            subcategory_name TEXT NOT NULL,
            subcategory_image TEXT,
            price REAL,
            FOREIGN KEY (parent_id) REFERENCES categories(category_id)
        )
    ''')

    # Orders table to save each order after checkout
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            order_total REAL NOT NULL,
            order_details TEXT NOT NULL,
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Chat conversations table: each conversation saved as one row
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_conversations (
            conversation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            conversation TEXT,
            conversation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP            
        )
    ''')

    db.commit()

    # Create default admin if not exists
    admin_exists = db.execute("SELECT * FROM users WHERE role='admin'").fetchone()
    if not admin_exists:
        db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                   ("admin", "admin123", "admin"))
        db.commit()


# ------------------------------------------------------------------------------
# Role Helper
# ------------------------------------------------------------------------------
def is_admin():
    return 'username' in session and session.get('role') == 'admin'


# ------------------------------------------------------------------------------
# Example Routes (Index, Category Details, Login, Register, Logout)
# ------------------------------------------------------------------------------
@app.route('/')
def index():
    # Clear any previous conversation so that a new session is started
    session.pop('current_conversation_id', None)
    db = get_db()
    categories = db.execute("SELECT * FROM categories").fetchall()
    return render_template('index.html', categories=categories)


@app.route('/category/<int:category_id>')
def category_details(category_id):
    db = get_db()
    category = db.execute("SELECT * FROM categories WHERE category_id=?", (category_id,)).fetchone()
    if not category:
        return "Category not found.", 404
    subcategories = db.execute("SELECT * FROM subcategories WHERE parent_id=?", (category_id,)).fetchall()
    return render_template('category_details.html', category=category, subcategories=subcategories)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and user["password"] == password:
            session['username'] = user["username"]
            session['role'] = user["role"]
            # Optionally, clear any existing conversation ID when a user logs in
            session.pop('current_conversation_id', None)
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="Invalid username or password.")
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        db = get_db()
        existing = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if existing:
            return render_template('register.html', error="Username already exists.")
        db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                   (username, password, "user"))
        db.commit()
        session['username'] = username
        session['role'] = "user"
        # Optionally, clear any existing conversation ID
        session.pop('current_conversation_id', None)
        return redirect(url_for('index'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.pop('username', None)
    session.pop('role', None)
    session.pop('current_conversation_id', None)  # clear conversation if any
    return redirect(url_for('index'))


# ------------------------------------------------------------------------------
# Shopping Cart & Checkout (Cart stored in session)
# ------------------------------------------------------------------------------
@app.route('/add_to_cart', methods=['POST'])
def add_to_cart():
    if 'cart' not in session:
        session['cart'] = {}
    subcat_id = request.form.get('subcategory_id')
    quantity = request.form.get('quantity', 1, type=int)
    if subcat_id:
        str_id = str(subcat_id)
        session['cart'][str_id] = session['cart'].get(str_id, 0) + quantity
        session.modified = True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return {"status": "success", "message": "Product added to cart."}
    else:
        return redirect(url_for('cart'))


@app.route('/cart')
def cart():
    db = get_db()
    cart_items = []
    total_price = 0.0
    if 'cart' not in session or len(session['cart']) == 0:
        return render_template('cart.html', cart_items=[], total=0.0)
    for str_subcat_id, qty in session['cart'].items():
        subcat_id = int(str_subcat_id)
        row = db.execute("SELECT * FROM subcategories WHERE subcategory_id=?", (subcat_id,)).fetchone()
        if row:
            price = row['price'] if row['price'] else 0.0
            subtotal = price * qty
            total_price += subtotal
            cart_items.append({
                'subcategory_id': subcat_id,
                'name': row['subcategory_name'],
                'price': price,
                'quantity': qty,
                'subtotal': subtotal,
                'image': row['subcategory_image']
            })
    return render_template('cart.html', cart_items=cart_items, total=total_price)


@app.route('/update_cart', methods=['POST'])
def update_cart():
    if 'cart' not in session:
        return redirect(url_for('cart'))
    subcat_id = request.form.get('subcategory_id')
    new_qty = request.form.get('new_quantity', 1, type=int)
    if subcat_id in session['cart']:
        if new_qty > 0:
            session['cart'][subcat_id] = new_qty
        else:
            session['cart'].pop(subcat_id)
        session.modified = True
    return redirect(url_for('cart'))


@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    db = get_db()
    if request.method == 'POST':
        cart_items = []
        total_price = 0.0
        if 'cart' in session:
            for str_subcat_id, qty in session['cart'].items():
                subcat_id = int(str_subcat_id)
                row = db.execute("SELECT * FROM subcategories WHERE subcategory_id=?", (subcat_id,)).fetchone()
                if row:
                    item = {
                        'subcategory_id': subcat_id,
                        'name': row['subcategory_name'],
                        'price': row['price'] if row['price'] else 0.0,
                        'quantity': qty,
                        'subtotal': (row['price'] if row['price'] else 0.0) * qty,
                        'image': row['subcategory_image']
                    }
                    cart_items.append(item)
                    total_price += item['subtotal']
        order_details = json.dumps(cart_items)
        username = session.get('username', 'guest')
        db.execute("INSERT INTO orders (username, order_total, order_details) VALUES (?, ?, ?)",
                   (username, total_price, order_details))
        db.commit()
        session.pop('cart', None)
        return render_template('checkout_success.html', total=total_price)
    return render_template('checkout.html')


# ------------------------------------------------------------------------------
# ADMIN: Manage Categories & Subcategories
# ------------------------------------------------------------------------------
@app.route('/admin/categories')
def admin_categories():
    if not is_admin():
        return redirect(url_for('index'))
    db = get_db()
    categories = db.execute("SELECT * FROM categories").fetchall()
    subcategories = db.execute("SELECT * FROM subcategories").fetchall()
    return render_template('admin_categories.html', categories=categories, subcategories=subcategories)


@app.route('/admin/delete_category/<int:category_id>', methods=['POST'])
def delete_category(category_id):
    if not is_admin():
        return redirect(url_for('index'))
    db = get_db()
    db.execute("DELETE FROM subcategories WHERE parent_id=?", (category_id,))
    db.execute("DELETE FROM categories WHERE category_id=?", (category_id,))
    db.commit()
    return redirect(url_for('admin_categories'))


@app.route('/admin/delete_subcategory/<int:subcategory_id>', methods=['POST'])
def delete_subcategory(subcategory_id):
    if not is_admin():
        return redirect(url_for('index'))
    db = get_db()
    db.execute("DELETE FROM subcategories WHERE subcategory_id=?", (subcategory_id,))
    db.commit()
    return redirect(url_for('admin_categories'))


# ------------------------------------------------------------------------------
# ADMIN: Add Category (Separate Page)
# ------------------------------------------------------------------------------
@app.route('/admin/add_category', methods=['GET', 'POST'])
def add_category_route():
    if not is_admin():
        return redirect(url_for('index'))
    if request.method == 'POST':
        category_name = request.form.get('categoryName')
        file = request.files.get('categoryImage')
        image_path = None
        if file:
            try:
                filename = secure_filename(file.filename)
                unique_name = str(uuid.uuid4()) + ".jpg"
                save_path = os.path.join(UPLOAD_FOLDER, unique_name)
                image = Image.open(file)
                image = image.convert("RGB")
                image.save(save_path, "JPEG")
                image_path = "uploads/" + unique_name
            except Exception as e:
                print(f"Error saving category image: {e}")
                image_path = None
        db = get_db()
        db.execute("INSERT INTO categories (category_name, image_path) VALUES (?, ?)",
                   (category_name, image_path))
        db.commit()
        return redirect(url_for('admin_categories'))
    return render_template('admin_add_category.html')


# ------------------------------------------------------------------------------
# ADMIN: Add Subcategory (Separate Page, with Price)
# ------------------------------------------------------------------------------
@app.route('/admin/add_subcategory', methods=['GET', 'POST'])
def add_subcategory_route():
    if not is_admin():
        return redirect(url_for('index'))
    db = get_db()
    if request.method == 'POST':
        parent_id = request.form.get('parentCategory')
        subcategory_name = request.form.get('subCategoryName')
        price = request.form.get('subCategoryPrice')
        file = request.files.get('subcategoryImage')
        subcategory_image = None
        if file:
            try:
                filename = secure_filename(file.filename)
                unique_name = str(uuid.uuid4()) + ".jpg"
                save_path = os.path.join(UPLOAD_FOLDER, unique_name)
                image = Image.open(file)
                image = image.convert("RGB")
                image.save(save_path, "JPEG")
                subcategory_image = "uploads/" + unique_name
            except Exception as e:
                print(f"Error saving subcategory image: {e}")
                subcategory_image = None
        db.execute(
            "INSERT INTO subcategories (parent_id, subcategory_name, subcategory_image, price) VALUES (?, ?, ?, ?)",
            (parent_id, subcategory_name, subcategory_image, price))
        db.commit()
        return redirect(url_for('admin_categories'))
    categories = db.execute("SELECT * FROM categories").fetchall()
    return render_template('admin_add_subcategory.html', categories=categories)


# ------------------------------------------------------------------------------
# ADMIN: View Orders (Admin Only) - Show only names and quantities
# ------------------------------------------------------------------------------
@app.route('/admin/orders')
def admin_orders():
    if not is_admin():
        return redirect(url_for('index'))
    db = get_db()
    orders = db.execute("SELECT * FROM orders ORDER BY order_date DESC").fetchall()
    orders_list = []
    import json
    for order in orders:
        order_dict = dict(order)
        try:
            details = json.loads(order_dict.get('order_details', '[]'))
            filtered = [{"name": item.get("name", "Unknown"), "quantity": item.get("quantity", 0)} for item in details]
        except Exception as e:
            print(f"Error parsing order details: {e}")
            filtered = []
        order_dict["parsed_details"] = filtered
        orders_list.append(order_dict)
    return render_template('admin_orders.html', orders=orders_list)


# ------------------------------------------------------------------------------
# Chat Conversation Saving Route
# ------------------------------------------------------------------------------
@app.route('/save_conversation', methods=['POST'])
def save_conversation():
    data = request.get_json()
    new_messages = data.get('conversation', '')
    # Remove empty lines from new_messages
    new_messages_cleaned = "\n".join(line for line in new_messages.split("\n") if line.strip())
    if not new_messages_cleaned:
        return {"status": "success", "message": "No new messages to save"}

    username = session.get('username', 'guest')
    db = get_db()

    if 'current_conversation_id' not in session or session.get('current_conversation_id') == "unknown":
        # Insert new conversation with cleaned messages
        cur = db.execute("INSERT INTO chat_conversations (username, conversation) VALUES (?, ?)",
                         (username, new_messages_cleaned))
        conversation_id = cur.lastrowid
        session['current_conversation_id'] = conversation_id
    else:
        conversation_id = session['current_conversation_id']
        # Append the new cleaned messages (preceded by a newline) to the existing conversation
        db.execute(
            "UPDATE chat_conversations SET conversation = COALESCE(conversation, '') || '\n' || ? WHERE conversation_id = ?",
            (new_messages_cleaned, conversation_id))

    db.commit()
    return {"status": "success"}


@app.route('/save_recording', methods=['POST'])
def save_recording():
    if 'recording' not in request.files:
        return {"status": "error", "message": "No recording provided"}, 400

    file = request.files['recording']
    username = session.get('username', 'guest')
    db = get_db()

    # Ensure a valid conversation exists; if not, create one.
    if 'current_conversation_id' not in session or session.get('current_conversation_id') == "unknown":
        cur = db.execute("INSERT INTO chat_conversations (username, conversation) VALUES (?, ?)",
                         (username, ""))
        conversation_id = cur.lastrowid
        session['current_conversation_id'] = conversation_id
    else:
        conversation_id = session['current_conversation_id']

    # Determine next sequential number for this conversation
    pattern = os.path.join(RECORDS_FOLDER, f"{username}-{conversation_id}-*.mp3")
    existing_files = glob.glob(pattern)
    next_num = len(existing_files) + 1

    filename = f"{username}-{conversation_id}-{next_num}.mp3"
    file_path = os.path.join(RECORDS_FOLDER, filename)
    file.save(file_path)

    # Transcribe the audio file using Whisper
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(file_path)
        transcribed_text = result["text"].strip()
        print(f"Transcribed text: {transcribed_text}")
    except Exception as e:
        print("Error transcribing audio:", e)
        transcribed_text = ""

    # Normalize transcription: remove punctuation and spaces, lowercase it
    import string
    normalized_text = transcribed_text.lower().translate(str.maketrans('', '', string.punctuation)).replace(" ", "")

    if normalized_text == "checkout":
        # Process checkout
        cart_items = []
        total_price = 0.0
        if 'cart' in session:
            for str_subcat_id, qty in session['cart'].items():
                subcat_id = int(str_subcat_id)
                row = db.execute("SELECT * FROM subcategories WHERE subcategory_id=?", (subcat_id,)).fetchone()
                if row:
                    price = row['price'] if row['price'] else 0.0
                    subtotal = price * qty
                    total_price += subtotal
                    cart_items.append({
                        'name': row['subcategory_name'],
                        'price': price,
                        'quantity': qty,
                        'image': row['subcategory_image']
                    })
        if total_price > 0:
            order_details = json.dumps(cart_items)
            db.execute("INSERT INTO orders (username, order_total, order_details) VALUES (?, ?, ?)",
                       (username, total_price, order_details))
            db.commit()
            session.pop('cart', None)
            bot_reply = f"Your order has been placed. Total: ${total_price:.2f}."
        else:
            bot_reply = "Your cart is empty."
        return {"status": "success", "message": bot_reply, "order_items": cart_items, "file": filename}
    else:
        # Otherwise, perform product lookup for the transcribed text.
        bot_reply = ""
        if transcribed_text:
            subcats = db.execute(
                "SELECT subcategory_id, subcategory_name, price, subcategory_image FROM subcategories").fetchall()
            found_products = []
            for row in subcats:
                if row["subcategory_name"].lower() in transcribed_text.lower():
                    found_products.append({
                        'id': row['subcategory_id'],
                        'name': row['subcategory_name'],
                        'price': row['price'],
                        'image': row['subcategory_image']
                    })
            if found_products:
                product = found_products[0]
                if 'cart' not in session:
                    session['cart'] = {}
                session['cart'][str(product['id'])] = session['cart'].get(str(product['id']), 0) + 1
                session.modified = True
                bot_reply = f"Added {product['name']} (Price: ${product['price']}) to your cart."
            else:
                bot_reply = "I'm sorry, I couldn't find that product in our supermarket."
        else:
            bot_reply = "Sorry, I couldn't transcribe your recording."
        db.execute(
            "UPDATE chat_conversations SET conversation = COALESCE(conversation, '') || '\n' || ? WHERE conversation_id = ?",
            (bot_reply, conversation_id))
        db.commit()
        return {"status": "success", "message": bot_reply, "file": filename}


@app.route('/records/<path:filename>')
def get_recording(filename):
    return send_from_directory("records", filename)


@app.route('/my_own_ai', methods=['POST'])
def my_own_ai():
    data = request.get_json()
    user_message = data.get('user_message', '').lower()
    db = get_db()
    # Get all subcategories from the database
    subcats = db.execute("SELECT subcategory_id, subcategory_name, price, subcategory_image FROM subcategories").fetchall()
    found_products = []
    for row in subcats:
        if row['subcategory_name'].lower() in user_message:
            found_products.append({
                'id': row['subcategory_id'],
                'name': row['subcategory_name'],
                'price': row['price'],
                'image': row['subcategory_image']
            })
    if found_products:
        product = found_products[0]
        # Add product to the cart stored in session
        if 'cart' not in session:
            session['cart'] = {}
        session['cart'][str(product['id'])] = session['cart'].get(str(product['id']), 0) + 1
        session.modified = True
        bot_reply = f"Added {product['name']} (Price: ${product['price']}) to your cart."
        return {"bot_reply": bot_reply, "product_image": product['image']}
    else:
        return {"bot_reply": "I'm sorry, I couldn't find that product in our supermarket."}


@app.route('/api/get_cart', methods=['GET'])
def api_get_cart():
    db = get_db()
    cart_items = []
    total_price = 0.0
    if 'cart' in session:
        for str_subcat_id, qty in session['cart'].items():
            subcat_id = int(str_subcat_id)
            row = db.execute("SELECT * FROM subcategories WHERE subcategory_id=?", (subcat_id,)).fetchone()
            if row:
                price = row['price'] if row['price'] else 0.0
                subtotal = price * qty
                total_price += subtotal
                cart_items.append({
                    'subcategory_id': subcat_id,
                    'name': row['subcategory_name'],
                    'price': price,
                    'quantity': qty,
                    'subtotal': subtotal,
                    'image': row['subcategory_image']
                })
    return {"cart_items": cart_items, "total": total_price}


@app.route('/chat_checkout', methods=['POST'])
def chat_checkout():
    db = get_db()
    cart_items = []
    total_price = 0.0
    if 'cart' in session:
        for str_subcat_id, qty in session['cart'].items():
            subcat_id = int(str_subcat_id)
            row = db.execute("SELECT * FROM subcategories WHERE subcategory_id=?", (subcat_id,)).fetchone()
            if row:
                price = row['price'] if row['price'] else 0.0
                subtotal = price * qty
                total_price += subtotal
                cart_items.append({
                    'subcategory_id': subcat_id,
                    'name': row['subcategory_name'],
                    'price': price,
                    'quantity': qty,
                    'subtotal': subtotal,
                    'image': row['subcategory_image']
                })
    if total_price > 0:
        username = session.get('username', 'guest')
        order_details = json.dumps(cart_items)
        db.execute("INSERT INTO orders (username, order_total, order_details) VALUES (?, ?, ?)",
                   (username, total_price, order_details))
        db.commit()
        session.pop('cart', None)
        message = f"Your order has been placed. Total: ${total_price:.2f}."
    else:
        message = "Your cart is empty."
    return {"status": "success", "message": message, "order_items": cart_items}

@app.route('/get_user_orders', methods=['GET'])
def get_user_orders():
    db = get_db()
    username = session.get('username', 'guest')
    orders = db.execute("SELECT * FROM orders WHERE username=? ORDER BY order_date DESC", (username,)).fetchall()
    orders_list = []
    for order in orders:
        order_dict = dict(order)
        try:
            details = json.loads(order_dict.get('order_details', '[]'))
        except Exception as e:
            details = []
        order_dict['details'] = details
        orders_list.append(order_dict)
    return {"orders": orders_list}


# ------------------------------------------------------------------------------
# Main Launch
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    if not os.path.exists(DATABASE):
        open(DATABASE, 'w').close()
    with app.app_context():
        init_db()
    app.run(debug=True)
