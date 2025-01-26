CREATE USER swiftnotesadmin WITH PASSWORD 'swiftnotespass';

-- Grant connect privilege
GRANT CONNECT ON DATABASE swiftnotes TO swiftnotesadmin;

-- Grant all privileges on the specified database and all schema items within public
GRANT ALL PRIVILEGES ON DATABASE swiftnotes TO swiftnotesadmin;

GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO swiftnotesadmin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO swiftnotesadmin;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO swiftnotesadmin;

--Grant default privileges for new objects created in the public schema
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO swiftnotesadmin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO swiftnotesadmin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO swiftnotesadmin;



CREATE TABLE visitor_notes (
    visitor_id UUID NOT NULL,  
    youtube_video_id TEXT NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (visitor_id, youtube_video_id)
);

CREATE TABLE webhook_logs (
    id SERIAL PRIMARY KEY,
    stripe_event_id TEXT,
    stripe_customer_id TEXT,
    event_type TEXT NOT NULL,
    event_data JSONB,
    processing_status TEXT DEFAULT 'pending',
    processing_details TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP WITH TIME ZONE
);

-- Index for faster lookups
-- CREATE INDEX idx_webhook_logs_event_id ON webhook_logs(stripe_event_id);
-- CREATE INDEX idx_webhook_logs_created_at ON webhook_logs(created_at);
-- CREATE INDEX idx_webhook_logs_customer_id ON webhook_logs(stripe_customer_id);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth0_id TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    stripe_customer_id TEXT UNIQUE,
    subscription_id TEXT,
    subscription_status TEXT NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);
