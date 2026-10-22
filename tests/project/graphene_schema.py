"""A Graphene schema for the graphene-django tests: the Strawberry one's users, a failing field, and a mutation."""

import graphene
from django.contrib.auth.models import Group, User
from graphene_django import DjangoObjectType


class UserType(DjangoObjectType):
    groups = graphene.List(graphene.NonNull(graphene.String), required=True)

    class Meta:
        model = User
        fields = ("id", "username")

    @staticmethod
    def resolve_groups(user: User, _info: graphene.ResolveInfo) -> list[str]:
        # One query per user: an N+1 that a DataLoader would batch.
        return [group.name for group in Group.objects.filter(user__id=user.id)]


class Query(graphene.ObjectType):
    users = graphene.List(graphene.NonNull(UserType), required=True)
    broken = graphene.String(required=True)

    def resolve_users(self, info: graphene.ResolveInfo) -> list[User]:
        return list(User.objects.order_by("id"))

    def resolve_broken(self, info: graphene.ResolveInfo) -> str:
        msg = "the reporting service is down"
        raise RuntimeError(msg)


class CreateGroup(graphene.Mutation):
    class Arguments:
        name = graphene.String(required=True)

    name = graphene.String(required=True)

    @staticmethod
    def mutate(_root: object, _info: graphene.ResolveInfo, name: str) -> Group:
        return Group.objects.create(name=name)


class Mutation(graphene.ObjectType):
    create_group = CreateGroup.Field()


schema = graphene.Schema(query=Query, mutation=Mutation)
