from flask import Blueprint, request, jsonify, g, send_file, make_response, current_app
import logging
import re
import os
import boto3
import tempfile
from xhtml2pdf import pisa
import fitz  # PyMuPDF
import io
import zipfile
import requests
from youtube_transcript_api import YouTubeTranscriptApi
import google.generativeai as genai
from authlib.jose import jwt
import json
import time
import psycopg2
import psycopg2.extras
from services.auth_service import auth0_validator, AUTH0_DOMAIN, public_endpoint
import stripe
from services.database import get_db_connection

payments_bp = Blueprint('payments', __name__)

# The webhook secret and API key will be accessed from current_app.config when needed
@payments_bp.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    # Add debug logging to verify the request is reaching this point
    logging.info("Received Stripe webhook request")
    
    # Get configuration from current_app
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    stripe_endpoint_secret = current_app.config['STRIPE_WEBHOOK_SECRET']
    
    # Log headers for debugging
    logging.info(f"Webhook Headers: {dict(request.headers)}")
    
    logging.info("Stripe webhook received")
    payload = request.data.decode("utf-8")
    signature = request.headers.get('Stripe-Signature')
    webhook_log_id = None  
    
    try:
        # Verify Stripe signature
        event = stripe.Webhook.construct_event(payload, signature, stripe_endpoint_secret)
        
        # Extract customer ID from the event
        customer_id = event.data.object.customer

        # Log the webhook event with customer ID
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO webhook_logs 
                (stripe_event_id, event_type, event_data, stripe_customer_id, created_at)
                VALUES (%s, %s, %s, %s, NOW())
                RETURNING id
            """, (
                event.id,
                event.type,
                json.dumps(event.data.object),
                customer_id
            ))
            webhook_log_id = cur.fetchone()[0]
        conn.commit()
        
        # Process the event with retries
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                if event.type == 'customer.subscription.created':
                    subscription = event.data.object
                    email = stripe.Customer.retrieve(subscription.customer).email
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE users 
                            SET subscription_status = 'ACTIVE',
                                subscription_id = %s,
                                stripe_customer_id = %s,
                                updated_at = NOW()
                            WHERE email = %s
                        """, (subscription.id, subscription.customer, email))
                        
                        # Update webhook log with processing status
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Subscription activated',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"New subscription created for customer {subscription.customer}")
                    
                elif event.type == 'invoice.paid':
                    invoice = event.data.object
                    subscription = stripe.Subscription.retrieve(invoice.subscription)
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Payment confirmed and subscription extended',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"Payment confirmed for customer {invoice.customer}")
                    
                elif event.type == 'customer.subscription.updated':
                    subscription = event.data.object
                    
                    if subscription.cancel_at_period_end == False:
                        # Handle subscription renewal (existing code)
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE users 
                                SET subscription_status = 'ACTIVE',
                                    subscription_cancelled_at = NULL,
                                    subscription_cancelled_period_ends_at = NULL,
                                    updated_at = NOW()
                                WHERE stripe_customer_id = %s
                                  AND subscription_cancelled_at IS NOT NULL
                            """, (subscription.customer,))
                            
                            if cur.rowcount > 0:
                                cur.execute("""
                                    UPDATE webhook_logs 
                                    SET processing_status = 'success',
                                        processing_details = 'Subscription renewed',
                                        processed_at = NOW()
                                    WHERE id = %s
                                """, (webhook_log_id,))
                                logging.info(f"Subscription renewed for customer {subscription.customer}")
                    
                    elif subscription.cancel_at_period_end == True:
                        # Handle subscription cancellation
                        conn = get_db_connection()
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE users 
                                SET subscription_cancelled_at = NOW(),
                                    subscription_cancelled_period_ends_at = to_timestamp(%s),
                                    updated_at = NOW()
                                WHERE stripe_customer_id = %s
                            """, (subscription.current_period_end, subscription.customer))
                            
                            cur.execute("""
                                UPDATE webhook_logs 
                                SET processing_status = 'success',
                                    processing_details = 'Subscription cancelled (will end at period end)',
                                    processed_at = NOW()
                                WHERE id = %s
                            """, (webhook_log_id,))
                            logging.info(f"Subscription cancelled (pending end of period) for customer {subscription.customer}")
                    
                    conn.commit()

                elif event.type == 'invoice.payment_failed':
                    invoice = event.data.object
                    attempt_count = invoice.attempt_count
                    
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        # After 3 failed attempts, mark subscription as past_due
                        new_status = 'INACTIVE' if attempt_count >= 3 else 'ACTIVE'
                        cur.execute("""
                            UPDATE users 
                            SET subscription_status = %s,
                                updated_at = NOW()
                            WHERE stripe_customer_id = %s
                        """, (new_status, invoice.customer))
                        
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = %s,
                                processed_at = NOW()
                            WHERE id = %s
                        """, (f"Payment failed (attempt {attempt_count})", webhook_log_id))
                    conn.commit()
                    
                    # TODO: Send email notification about failed payment
                    logging.error(f"Payment failed for customer {invoice.customer} (attempt {attempt_count})")
                    
                elif event.type == 'customer.subscription.deleted':
                    subscription = event.data.object
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE users 
                            SET subscription_status = 'INACTIVE'
                            WHERE stripe_customer_id = %s
                        """, (subscription.customer,))
                        
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = 'success',
                                processing_details = 'Subscription cancelled and terminated',
                                processed_at = NOW()
                            WHERE id = %s
                        """, (webhook_log_id,))
                    conn.commit()
                    logging.info(f"Subscription terminated for customer {subscription.customer}")
                
                # If we get here, processing succeeded
                break
                
            except Exception as e:
                retry_count += 1
                error_msg = str(e)
                
                try:
                    conn = get_db_connection()
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE webhook_logs 
                            SET processing_status = %s,
                                processing_details = %s,
                                processed_at = NOW()
                            WHERE id = %s
                        """, (
                            'error' if retry_count == max_retries else 'retrying',
                            f"Error: {error_msg} (attempt {retry_count}/{max_retries})",
                            webhook_log_id
                        ))
                    conn.commit()
                except Exception as log_error:
                    logging.error(f"Failed to update webhook log: {str(log_error)}")
                
                if retry_count == max_retries:
                    logging.error(f"Failed to process webhook after {max_retries} attempts: {error_msg}")
                    return jsonify({'error': 'Processing failed'}), 500
                    
                time.sleep(2 ** retry_count)
                continue
        
        return jsonify({'message': 'Webhook processed'}), 200
        
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        error_msg = str(e)
        logging.error(f"Webhook verification failed: {error_msg}")
        
        # Log verification failure
        try:
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO webhook_logs 
                    (event_type, processing_status, processing_details, created_at, processed_at)
                    VALUES (%s, %s, %s, NOW(), NOW())
                """, (
                    'verification_failed',
                    'error',
                    f"Verification error: {error_msg}"
                ))
            conn.commit()
        except Exception as log_error:
            logging.error(f"Failed to log webhook verification error: {str(log_error)}")
            
        return jsonify({'error': error_msg}), 400

@payments_bp.route('/cancel_subscription', methods=['POST'])
def cancel_subscription():
    # Check for Bearer token
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'No authentication token provided'}), 401

    token = auth_header.split(' ')[1]
    max_retries = 3
    base_delay = 1  # Base delay in seconds

    # Function to handle retries with exponential backoff
    def retry_operation(operation, *args, **kwargs):
        for attempt in range(max_retries):
            try:
                return operation(*args, **kwargs)
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    raise  # Re-raise the last exception
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.warning(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {delay} seconds...")
                time.sleep(delay)

    try:
        # Verify token and get user info
        def verify_token():
            claims = auth0_validator.validate_token(token, scopes=None, request=None)
            auth0_id = claims['sub']
            return auth0_id

        try:
            auth0_id = retry_operation(verify_token)
        except jwt.InvalidTokenError as e:
            logging.error(f"Invalid JWT token: {str(e)}")
            return jsonify({'error': 'Invalid authentication token'}), 401
        except Exception as e:
            logging.error(f"Error verifying token: {type(e).__name__}: {str(e)}")
            return jsonify({'error': 'Authentication error'}), 401

        # Get user's subscription info from database
        def get_user_subscription():
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT subscription_id, stripe_customer_id
                    FROM users 
                    WHERE auth0_id = %s
                """, (auth0_id,))
                return cur.fetchone()

        try:
            user = retry_operation(get_user_subscription)
            if not user or not user['subscription_id']:
                return jsonify({'error': 'No active subscription found'}), 404

        except Exception as e:
            logging.error(f"Database error getting user subscription: {str(e)}")
            return jsonify({'error': 'Internal server error'}), 500

        # Cancel the subscription with Stripe
        def cancel_stripe_subscription():
            return stripe.Subscription.modify(
                user['subscription_id'],
                cancel_at_period_end=True
            )

        try:
            subscription = retry_operation(cancel_stripe_subscription)
        except stripe.error.StripeError as e:
            logging.error(f"Stripe error: {str(e)}")
            return jsonify({'error': 'Failed to cancel subscription'}), 500

        # Update database with cancellation info
        def update_user_cancellation():
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE users 
                    SET subscription_cancelled_at = NOW(),
                        subscription_cancelled_period_ends_at = to_timestamp(%s)
                    WHERE auth0_id = %s
                """, (subscription.current_period_end, auth0_id))
                conn.commit()

        try:
            retry_operation(update_user_cancellation)
        except Exception as e:
            logging.error(f"Database error updating cancellation info: {str(e)}")
            # Note: Subscription is already cancelled in Stripe at this point
            return jsonify({'error': 'Subscription cancelled but failed to update database'}), 500

        return jsonify({
            'message': 'Subscription will be canceled at the end of the billing period',
            'current_period_end': subscription.current_period_end,
        }), 200

    except Exception as e:
        logging.error(f"Unexpected error in cancel_subscription: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@payments_bp.route('/manage_sub', methods=['POST'])
def manage_subscription():
    # Check for Bearer token
    stripe.api_key = current_app.config['STRIPE_SECRET_KEY']
    auth_header = request.headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({'error': 'No authentication token provided'}), 401

    token = auth_header.split(' ')[1]
    try:
        decoded_token = jwt.decode(
        token,
        auth0_validator.public_key,  # Use the public key from your validator
        claims_options={
            "aud": {"essential": True, "value": AUTH0_DOMAIN},
            "iss": {"essential": True, "value": f'https://{AUTH0_DOMAIN}/'}
            }
        )

        auth0_id = decoded_token['sub']

        # Get user's Stripe customer ID from database
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT stripe_customer_id
                FROM users 
                WHERE auth0_id = %s
            """, (auth0_id,))
            user = cur.fetchone()

            if not user or not user['stripe_customer_id']:
                return jsonify({'error': 'No Stripe customer found'}), 404
            
            try:
                # Create Stripe billing portal session
                session = stripe.billing_portal.Session.create(
                    customer=user['stripe_customer_id'],
                )
                return jsonify({'url': session.url}), 200

            except stripe.error.StripeError as e:
                logging.error(f"Stripe error creating portal session: {str(e)}")
                return jsonify({'error': 'Failed to create management session'}), 500

    except jwt.InvalidTokenError as e:
        logging.error(f"Invalid JWT token: {str(e)}")
        return jsonify({'error': 'Invalid authentication token'}), 401
    except Exception as e:
        logging.error(f"Error in manage_subscription: {type(e).__name__}: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500