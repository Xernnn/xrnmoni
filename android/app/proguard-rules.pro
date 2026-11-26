# kotlinx.serialization: the generated serializers of the app's own @Serializable classes
# (the outbox lines, the tracker's saved state and the hub's answer).
-keepattributes RuntimeVisibleAnnotations,AnnotationDefault
-keepclassmembers @kotlinx.serialization.Serializable class com.xern.xrnmoni.** {
    *** Companion;
    kotlinx.serialization.KSerializer serializer(...);
}
-keepclasseswithmembers class com.xern.xrnmoni.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class com.xern.xrnmoni.**$$serializer { *; }
