def register_routes(app):
    from routes.user import user_bp
    from routes.notes import notes_bp
    from routes.payments import payments_bp
    from routes.quiz import quiz_bp
    from routes.search import search_bp
    from routes.feedback import feedback_bp
    
    app.register_blueprint(user_bp)
    app.register_blueprint(notes_bp)
    app.register_blueprint(payments_bp)
    app.register_blueprint(quiz_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(feedback_bp) 