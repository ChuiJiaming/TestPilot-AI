from datetime import datetime, timezone
import unittest
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apps.api.app.auth.persistence import AuthBase, UserRecord
from apps.api.app.tenancy.persistence import (
    ForbiddenScope,
    MissingResource,
    OrganizationMembershipRecord,
    OrganizationProjectRepository,
)


class OrganizationProjectRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        AuthBase.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self.repository = OrganizationProjectRepository(self.sessions)
        self.now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.owner_id = UUID("11111111-1111-4111-8111-111111111111")
        self.member_id = UUID("22222222-2222-4222-8222-222222222222")
        self.outsider_id = UUID("33333333-3333-4333-8333-333333333333")
        with self.sessions.begin() as session:
            for user_id, email, name in (
                (self.owner_id, "owner@example.com", "Owner"),
                (self.member_id, "member@example.com", "Member"),
                (self.outsider_id, "outsider@example.com", "Outsider"),
            ):
                session.add(UserRecord(
                    id=user_id, email=email, password_hash="hash", display_name=name,
                    platform_role="user", status="active", created_at=self.now,
                    email_verified_at=self.now,
                ))

    def tearDown(self):
        self.engine.dispose()

    def test_project_creator_admin_and_org_admin_implicit_access(self):
        organization = self.repository.create_organization(
            self.owner_id, name="Acme", slug="acme", now=self.now
        )
        project = self.repository.create_project(
            self.owner_id,
            organization_id=organization["id"],
            name="Web",
            slug="web",
            now=self.now,
        )
        self.assertEqual(organization["role"], "org_admin")
        self.assertEqual(project["role"], "project_admin")

        with self.sessions.begin() as session:
            session.add(OrganizationMembershipRecord(
                organization_id=organization["id"], user_id=self.member_id,
                role="org_member", created_at=self.now,
            ))
        other_project = self.repository.create_project(
            self.member_id,
            organization_id=organization["id"],
            name="API",
            slug="api",
            now=self.now,
        )
        self.assertEqual(self.repository.get_project(self.owner_id, other_project["id"])["role"], "org_admin")
        with self.assertRaises(MissingResource):
            self.repository.get_project(self.outsider_id, project["id"])

    def test_membership_changes_are_scoped_and_remove_project_access_immediately(self):
        organization = self.repository.create_organization(
            self.owner_id, name="Acme", slug="acme", now=self.now
        )
        project = self.repository.create_project(
            self.owner_id,
            organization_id=organization["id"],
            name="Web",
            slug="web",
            now=self.now,
        )
        with self.sessions.begin() as session:
            session.add(OrganizationMembershipRecord(
                organization_id=organization["id"], user_id=self.member_id,
                role="org_member", created_at=self.now,
            ))
        self.repository.set_project_member(
            self.owner_id, project["id"], self.member_id, role="tester", now=self.now
        )
        self.assertEqual(len(self.repository.list_projects(self.member_id)), 1)
        self.repository.remove_project_member(self.owner_id, project["id"], self.member_id)
        self.assertEqual(self.repository.list_projects(self.member_id), [])
        with self.assertRaises(ForbiddenScope):
            self.repository.create_project(
                self.outsider_id,
                organization_id=organization["id"],
                name="Intrusion",
                slug="intrusion",
                now=self.now,
            )

    def test_organization_cursor_pages_only_organizations_visible_to_caller(self):
        self.repository.create_organization(self.owner_id, name="Acme", slug="acme", now=self.now)
        self.repository.create_organization(self.owner_id, name="Beta", slug="beta", now=self.now)
        first = self.repository.list_organizations(self.owner_id, limit=1)
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        second = self.repository.list_organizations(
            self.owner_id, limit=1, cursor=first["next_cursor"]
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["name"], "Beta")
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(self.repository.list_organizations(self.outsider_id, limit=1)["items"], [])

    def test_organization_member_listing_is_scoped_and_cursor_paged(self):
        organization = self.repository.create_organization(
            self.owner_id, name="Acme", slug="acme", now=self.now
        )
        with self.sessions.begin() as session:
            session.add(OrganizationMembershipRecord(
                organization_id=organization["id"], user_id=self.member_id,
                role="org_member", created_at=self.now,
            ))

        first = self.repository.list_organization_members(
            self.owner_id, organization["id"], limit=1
        )
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        self.assertEqual(first["items"][0]["organization_id"], organization["id"])
        second = self.repository.list_organization_members(
            self.owner_id, organization["id"], limit=1, cursor=first["next_cursor"]
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertEqual(second["items"][0]["role"], "org_admin")
        self.assertEqual(
            {item["role"] for item in first["items"] + second["items"]},
            {"org_admin", "org_member"},
        )
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(MissingResource):
            self.repository.list_organization_members(
                self.outsider_id, organization["id"], limit=10
            )


if __name__ == "__main__":
    unittest.main()
