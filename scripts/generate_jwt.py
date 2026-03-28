"""CLI script to generate test JWT tokens for development."""

import argparse
import sys

sys.path.insert(0, ".")
from src.services.auth_service import create_token


def main():
    parser = argparse.ArgumentParser(description="Generate a test JWT token")
    parser.add_argument("--role", default="finance", choices=["analyst", "finance", "hr", "c_level", "admin"])
    parser.add_argument("--user-id", default="test_user")
    parser.add_argument("--name", default="Test User")
    args = parser.parse_args()

    token = create_token(user_id=args.user_id, name=args.name, role=args.role)
    print(f"Role: {args.role}")
    print(f"Token: {token}")


if __name__ == "__main__":
    main()
