from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token['email'] = user.email
        token['user_type'] = user.user_type
        return token

    def validate(self, attrs):
        # on force SimpleJWT à utiliser email correctement
        attrs['username'] = attrs.get('email')
        return super().validate(attrs)