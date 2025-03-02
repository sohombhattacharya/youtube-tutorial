import boto3
import os
from dotenv import load_dotenv

def count_s3_notes():
    """
    Count the total number of objects in the S3 bucket with the notes/ prefix
    using credentials from the .env file.
    
    Returns:
        int: Total number of objects in the notes/ directory
    """
    # Load environment variables
    load_dotenv()
    
    # Get S3 credentials from environment
    bucket_name = os.getenv("S3_NOTES_BUCKET_NAME")
    aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
    
    if not all([bucket_name, aws_access_key, aws_secret_key]):
        raise ValueError("Missing required S3 credentials or bucket name in .env file")
    
    # Create S3 client
    s3_client = boto3.client(
        's3',
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key
    )
    
    # Count objects with pagination to handle large buckets
    total_count = 0
    prefix = "tldr/"
    paginator = s3_client.get_paginator('list_objects_v2')
    
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        if 'Contents' in page:
            total_count += len(page['Contents'])
    
    return total_count

if __name__ == "__main__":
    try:
        count = count_s3_notes()
        print(f"Total objects in notes/ directory: {count}")
    except Exception as e:
        print(f"Error: {str(e)}")
