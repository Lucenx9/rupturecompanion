#define _POSIX_C_SOURCE 200809L

#include <dbus/dbus.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define KWIN_SERVICE "org.kde.KWin.ScreenShot2"
#define KWIN_OBJECT "/org/kde/KWin/ScreenShot2"
#define KWIN_INTERFACE "org.kde.KWin.ScreenShot2"
#define MAX_IMAGE_BYTES (512ULL * 1024ULL * 1024ULL)

struct image_info
{
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint32_t format;
    bool raw;
    bool has_width;
    bool has_height;
    bool has_stride;
    bool has_format;
};

static bool read_metadata(DBusMessage *reply, struct image_info *info)
{
    DBusMessageIter root;
    if (!dbus_message_iter_init(reply, &root)
        || dbus_message_iter_get_arg_type(&root) != DBUS_TYPE_ARRAY)
    {
        return false;
    }

    DBusMessageIter entries;
    dbus_message_iter_recurse(&root, &entries);
    while (dbus_message_iter_get_arg_type(&entries) == DBUS_TYPE_DICT_ENTRY)
    {
        DBusMessageIter entry;
        dbus_message_iter_recurse(&entries, &entry);
        if (dbus_message_iter_get_arg_type(&entry) != DBUS_TYPE_STRING)
        {
            return false;
        }
        const char *key = NULL;
        dbus_message_iter_get_basic(&entry, &key);
        if (!dbus_message_iter_next(&entry)
            || dbus_message_iter_get_arg_type(&entry) != DBUS_TYPE_VARIANT)
        {
            return false;
        }

        DBusMessageIter value;
        dbus_message_iter_recurse(&entry, &value);
        const int value_type = dbus_message_iter_get_arg_type(&value);
        if (strcmp(key, "type") == 0 && value_type == DBUS_TYPE_STRING)
        {
            const char *type = NULL;
            dbus_message_iter_get_basic(&value, &type);
            info->raw = strcmp(type, "raw") == 0;
        }
        else if (value_type == DBUS_TYPE_UINT32)
        {
            uint32_t number = 0;
            dbus_message_iter_get_basic(&value, &number);
            if (strcmp(key, "width") == 0)
            {
                info->width = number;
                info->has_width = true;
            }
            else if (strcmp(key, "height") == 0)
            {
                info->height = number;
                info->has_height = true;
            }
            else if (strcmp(key, "stride") == 0)
            {
                info->stride = number;
                info->has_stride = true;
            }
            else if (strcmp(key, "format") == 0)
            {
                info->format = number;
                info->has_format = true;
            }
        }
        dbus_message_iter_next(&entries);
    }
    return info->raw && info->has_width && info->has_height && info->has_stride
        && info->has_format;
}

static bool wait_for_image(int descriptor, uint64_t expected, int timeout_ms)
{
    const struct timespec pause = {.tv_sec = 0, .tv_nsec = 10000000};
    const int attempts = timeout_ms / 10 + 1;
    for (int attempt = 0; attempt < attempts; ++attempt)
    {
        struct stat status;
        if (fstat(descriptor, &status) != 0)
        {
            return false;
        }
        if ((uint64_t)status.st_size == expected)
        {
            return true;
        }
        if ((uint64_t)status.st_size > expected)
        {
            return false;
        }
        nanosleep(&pause, NULL);
    }
    return false;
}

static DBusMessage *capture_workspace(DBusConnection *connection, int descriptor,
                                      int timeout_ms, DBusError *error)
{
    DBusMessage *request = dbus_message_new_method_call(
        KWIN_SERVICE, KWIN_OBJECT, KWIN_INTERFACE, "CaptureWorkspace");
    if (request == NULL)
    {
        return NULL;
    }

    DBusMessageIter arguments;
    DBusMessageIter options;
    dbus_message_iter_init_append(request, &arguments);
    if (!dbus_message_iter_open_container(&arguments, DBUS_TYPE_ARRAY, "{sv}",
                                          &options)
        || !dbus_message_iter_close_container(&arguments, &options)
        || !dbus_message_iter_append_basic(&arguments, DBUS_TYPE_UNIX_FD,
                                           &descriptor))
    {
        dbus_message_unref(request);
        return NULL;
    }

    DBusMessage *reply = dbus_connection_send_with_reply_and_block(
        connection, request, timeout_ms, error);
    dbus_message_unref(request);
    return reply;
}

int main(int argc, char **argv)
{
    if (argc != 3)
    {
        fprintf(stderr, "usage: %s OUTPUT_RAW TIMEOUT_MS\n", argv[0]);
        return 2;
    }
    char *timeout_end = NULL;
    const long timeout = strtol(argv[2], &timeout_end, 10);
    if (timeout_end == argv[2] || *timeout_end != '\0' || timeout < 1
        || timeout > 30000)
    {
        fprintf(stderr, "invalid timeout\n");
        return 2;
    }

    const int descriptor = open(argv[1], O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC,
                                S_IRUSR | S_IWUSR);
    if (descriptor < 0)
    {
        fprintf(stderr, "cannot open output: %s\n", strerror(errno));
        return 1;
    }

    DBusError error;
    dbus_error_init(&error);
    DBusConnection *connection = dbus_bus_get(DBUS_BUS_SESSION, &error);
    if (connection == NULL)
    {
        fprintf(stderr, "cannot connect to session bus: %s\n",
                error.message != NULL ? error.message : "unknown error");
        dbus_error_free(&error);
        close(descriptor);
        return 1;
    }

    DBusMessage *reply = capture_workspace(connection, descriptor, (int)timeout, &error);
    if (reply == NULL)
    {
        fprintf(stderr, "KWin capture failed: %s\n",
                error.message != NULL ? error.message : "no reply");
        dbus_error_free(&error);
        dbus_connection_unref(connection);
        close(descriptor);
        return 1;
    }

    struct image_info info = {0};
    if (!read_metadata(reply, &info))
    {
        fprintf(stderr, "KWin returned invalid metadata\n");
        dbus_message_unref(reply);
        dbus_connection_unref(connection);
        close(descriptor);
        return 1;
    }
    const uint64_t image_size = (uint64_t)info.stride * info.height;
    if (info.width == 0 || info.height == 0 || info.width > 32768
        || info.height > 32768 || info.stride < info.width * 4ULL
        || image_size == 0 || image_size > MAX_IMAGE_BYTES
        || (info.format != 4 && info.format != 5 && info.format != 6)
        || !wait_for_image(descriptor, image_size, (int)timeout))
    {
        fprintf(stderr, "KWin returned an incomplete or unsafe image\n");
        dbus_message_unref(reply);
        dbus_connection_unref(connection);
        close(descriptor);
        return 1;
    }

    printf("raw %u %u %u %u\n", info.width, info.height, info.stride, info.format);
    dbus_message_unref(reply);
    dbus_connection_unref(connection);
    close(descriptor);
    return 0;
}
