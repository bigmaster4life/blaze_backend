from django.contrib import admin
from .models import CancellationRecord


@admin.register(CancellationRecord)
class CancellationRecordAdmin(admin.ModelAdmin):
    list_display = ('id','created_at','ride','delivery','actor_role','reason','status_before')
    list_filter = ('actor_role','reason','status_before')
    readonly_fields = ('ride','delivery','actor','partner','actor_role','reason','note','status_before','context','created_at')

    def has_add_permission(self, request): return False
    def has_change_permission(self, request, obj=None): return False
    def has_delete_permission(self, request, obj=None): return False


from .models import CancellationAlert, CancellationReview

@admin.register(CancellationAlert)
class CancellationAlertAdmin(admin.ModelAdmin):
    list_display=('id','created_at','record','score','status')
    readonly_fields=('record','score','signals','metrics','status','version','created_at','updated_at')
    def has_add_permission(self,request): return False
    def has_change_permission(self,request,obj=None): return False
    def has_delete_permission(self,request,obj=None): return False

@admin.register(CancellationReview)
class CancellationReviewAdmin(admin.ModelAdmin):
    list_display=('id','created_at','alert','reviewer','status')
    readonly_fields=('alert','reviewer','previous_status','status','note','created_at')
    def has_add_permission(self,request): return False
    def has_change_permission(self,request,obj=None): return False
    def has_delete_permission(self,request,obj=None): return False
