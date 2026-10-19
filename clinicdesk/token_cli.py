"""Issue a caller token: python -m clinicdesk.token_cli <subject> <tenant> <role>"""
import os
import sys

from .auth import issue_token

if __name__ == "__main__":
    sub, tenant, role = sys.argv[1:4]
    print(issue_token(os.environ["CLINICDESK_SECRET"], sub, tenant, role))
